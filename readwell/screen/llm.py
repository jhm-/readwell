"""LLM screening backends: one call scores a batch of items against a set of topics.

- ``GeminiScreener`` speaks Gemini's REST API (``models/{model}:generateContent``) with a JSON
  response schema: exact model choice, thinking turned down, and full token counts.
- ``AnthropicScreener`` speaks the Anthropic Messages API and forces a tool call to get the same
  structure back. It works with Claude itself or with a Claude-to-Gemini proxy.
- ``OpenAIScreener`` speaks OpenAI-compatible chat completions with a strict JSON schema:
  OpenAI, xAI (Grok), OpenRouter, Ollama and most other hosts.

Adding a provider means one subclass with a ``_call`` method and an entry in ``BACKENDS``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import httpx

from ..config import LlmCfg
from .topics import Topic

log = logging.getLogger(__name__)


@dataclass
class ScreenInput:
    key: str            # caller's id for the item
    source: str
    title: str
    published: str | None
    text: str
    note: str | None = None
    summarize: bool = False


@dataclass
class ScreenResult:
    key: str
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    summary: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Example:
    vote: int        # +1 / -1
    title: str
    source: str


class LlmError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, usage: Usage | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.usage = usage or Usage()


SYSTEM = """You screen news and government documents for a reader who works in mineral exploration
in western and northern Canada. For each item, score how well it fits each topic below, from 0
(unrelated) to 10 (squarely on topic and material). Score on what the item actually reports, not on
words it happens to share with a topic. Respect each topic's "not" list.
{jurisdictions}
Return only topics scoring 3 or more for an item; an item that fits nothing gets an empty list.
"reason" is one plain sentence of at most 20 words saying what the item is and why it matters for
its best topic (or why it fits none). When an item asks for a summary, write 2-3 plain sentences:
what the document does, under which act or authority, and who or what it affects.

Topics:
{topics}"""


def _topic_block(t: Topic, examples: list[Example]) -> str:
    lines = [f"- {t.id}: {t.label}. {t.description.strip()}"]
    if t.include:
        lines.append("  Includes: " + "; ".join(t.include))
    if t.exclude:
        lines.append("  Not: " + "; ".join(t.exclude))
    good = [e for e in examples if e.vote > 0]
    bad = [e for e in examples if e.vote < 0]
    if good:
        lines.append("  Reader marked as relevant: " + "; ".join(f'"{e.title}" ({e.source})' for e in good))
    if bad:
        lines.append("  Reader marked as NOT relevant: " + "; ".join(f'"{e.title}" ({e.source})' for e in bad))
    return "\n".join(lines)


def build_prompt(items: list[ScreenInput], topics: list[Topic], jurisdictions: list[str],
                 examples: dict[str, list[Example]] | None = None) -> tuple[str, str]:
    """Returns (system instruction, user message)."""
    juris = (f"Items about these places matter more (a soft weighting, not a filter): {', '.join(jurisdictions)}."
             if jurisdictions else "")
    system = SYSTEM.format(jurisdictions=juris,
                           topics="\n".join(_topic_block(t, (examples or {}).get(t.id, [])) for t in topics))
    payload = []
    for n, it in enumerate(items, 1):
        d = {"id": f"i{n}", "source": it.source, "title": it.title, "text": it.text}
        if it.published:
            d["published"] = it.published[:10]
        if it.note:
            d["source_note"] = it.note
        if it.summarize:
            d["wants_summary"] = True
        payload.append(d)
    return system, "Items:\n" + json.dumps(payload, ensure_ascii=False)


def response_schema(topic_ids: list[str]) -> dict:
    """Plain JSON Schema for one batch's answer."""
    return {
        "type": "object",
        "properties": {"results": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "scores": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"topic": {"type": "string", "enum": topic_ids}, "score": {"type": "integer"}},
                    "required": ["topic", "score"],
                }},
                "reason": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["id", "scores", "reason"],
        }}},
        "required": ["results"],
    }


def _openapi(schema: dict) -> dict:
    """Gemini's responseSchema wants OpenAPI-style upper-case type names."""
    out = {}
    for k, v in schema.items():
        if k == "type":
            out[k] = v.upper()
        elif isinstance(v, dict):
            out[k] = {kk: _openapi(vv) if isinstance(vv, dict) else vv for kk, vv in v.items()} \
                if k == "properties" else _openapi(v)
        else:
            out[k] = v
    return out


def estimate_tokens(system: str, user: str, n_items: int) -> tuple[int, int]:
    return (len(system) + len(user)) // 4 + 50, 60 * n_items


class Screener:
    default_base = ""

    def __init__(self, cfg: LlmCfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.base = (cfg.base_url or self.default_base).rstrip("/")
        self.client = httpx.AsyncClient(timeout=cfg.timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self.client.aclose()

    @property
    def key(self) -> str:
        return os.environ.get(self.cfg.api_key_env or "", "")

    @property
    def configured(self) -> bool:
        """A key, or a non-default base URL (a proxy that adds its own key)."""
        return bool(self.key) or self.base != self.default_base

    async def screen(self, items: list[ScreenInput], topics: list[Topic], jurisdictions: list[str],
                     examples: dict[str, list[Example]] | None = None) -> tuple[list[ScreenResult], Usage]:
        system, user = build_prompt(items, topics, jurisdictions, examples)
        ids = [t.id for t in topics]
        parsed, usage = await self._call(system, user, response_schema(ids))
        by_short = {f"i{n}": it for n, it in enumerate(items, 1)}
        out: list[ScreenResult] = []
        for r in parsed:
            if not isinstance(r, dict):
                continue
            it = by_short.get(str(r.get("id")))
            if it is None:
                continue
            scores = {}
            for sc in r.get("scores") or []:
                tid, val = (sc.get("topic"), sc.get("score")) if isinstance(sc, dict) else (None, None)
                if tid in ids and isinstance(val, int | float):
                    scores[tid] = float(max(0, min(10, val)))
            summary = (r.get("summary") or "").strip() or None
            out.append(ScreenResult(it.key, scores, (r.get("reason") or "").strip()[:300],
                                    summary if it.summarize else None))
        return out, usage

    async def _call(self, system: str, user: str, schema: dict) -> tuple[list, Usage]:
        raise NotImplementedError

    async def _post(self, url: str, body: dict, headers: dict) -> httpx.Response:
        try:
            return await self.client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LlmError(f"LLM unreachable: {type(e).__name__}: {e}") from e

    @staticmethod
    def _raise_for(r: httpx.Response) -> None:
        if r.status_code >= 400:
            detail = r.text[:300].replace("\n", " ")
            retryable = r.status_code in (408, 429) or r.status_code >= 500
            raise LlmError(f"LLM HTTP {r.status_code}: {detail}", retryable=retryable)


class GeminiScreener(Screener):
    default_base = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, cfg: LlmCfg, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(cfg, transport)
        self._thinking = cfg.thinking_level

    async def _call(self, system: str, user: str, schema: dict) -> tuple[list, Usage]:
        url = f"{self.base}/models/{self.cfg.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["x-goog-api-key"] = self.key
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self.cfg.max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": _openapi(schema),
            },
        }
        for attempt in (1, 2):
            if self._thinking:
                body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": self._thinking}
            else:
                body["generationConfig"].pop("thinkingConfig", None)
            r = await self._post(url, body, headers)
            if r.status_code == 400 and self._thinking and "think" in r.text.lower() and attempt == 1:
                log.warning("model %s rejected thinkingLevel=%s; retrying without it", self.cfg.model, self._thinking)
                self._thinking = None
                continue
            break
        self._raise_for(r)
        try:
            data = r.json()
        except ValueError as e:
            raise LlmError("LLM returned non-JSON") from e
        meta = data.get("usageMetadata") or {}
        usage = Usage(int(meta.get("promptTokenCount") or 0),
                      int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0))
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return json.loads(text)["results"], usage
        except (KeyError, IndexError, TypeError, ValueError) as e:
            cands = data.get("candidates") or [{}]
            raise LlmError(f"unreadable LLM response ({cands[0].get('finishReason') or type(e).__name__})",
                           usage=usage) from e


class AnthropicScreener(Screener):
    default_base = "https://api.anthropic.com"
    TOOL = "record_screening"

    async def _call(self, system: str, user: str, schema: dict) -> tuple[list, Usage]:
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
        if self.key:
            headers["x-api-key"] = self.key
        body = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_output_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": self.TOOL, "description": "Record the screening result for every item.",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": self.TOOL},
        }
        r = await self._post(f"{self.base}/v1/messages", body, headers)
        self._raise_for(r)
        try:
            data = r.json()
        except ValueError as e:
            raise LlmError("LLM returned non-JSON") from e
        u = data.get("usage") or {}
        usage = Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0))
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                inp = block.get("input") or {}
                if isinstance(inp, dict) and isinstance(inp.get("results"), list):
                    return inp["results"], usage
        for block in data.get("content") or []:  # some proxies answer in text even when a tool is forced
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    return json.loads(block["text"])["results"], usage
                except (KeyError, TypeError, ValueError):
                    pass
        raise LlmError(f"unreadable LLM response (stop_reason {data.get('stop_reason')})", usage=usage)


class OpenAIScreener(Screener):
    default_base = "https://api.openai.com/v1"

    async def _call(self, system: str, user: str, schema: dict) -> tuple[list, Usage]:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        body = {
            "model": self.cfg.model,
            "temperature": 0,
            "max_tokens": self.cfg.max_output_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "screening", "schema": schema}},
        }
        r = await self._post(f"{self.base}/chat/completions", body, headers)
        self._raise_for(r)
        try:
            data = r.json()
        except ValueError as e:
            raise LlmError("LLM returned non-JSON") from e
        u = data.get("usage") or {}
        usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
        try:
            return json.loads(data["choices"][0]["message"]["content"])["results"], usage
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LlmError(f"unreadable LLM response ({type(e).__name__})", usage=usage) from e


BACKENDS: dict[str, type[Screener]] = {
    "anthropic": AnthropicScreener,
    "gemini": GeminiScreener,
    "openai": OpenAIScreener,
}


def make_screener(cfg: LlmCfg, transport: httpx.AsyncBaseTransport | None = None) -> Screener | None:
    cls = BACKENDS.get(cfg.backend)
    return cls(cfg, transport) if cls else None
