import asyncio
import json

import httpx
import pytest
import yaml

from readwell import db
from readwell.config import LlmCfg
from readwell.screen import prefilter
from readwell.screen.llm import GeminiScreener, OpenAIScreener, ScreenInput, build_prompt
from readwell.screen.topics import Topic
from readwell.worker import Worker

from .conftest import write_sources

TOPICS = {
    "defaults": {"jurisdictions": ["Yukon"], "threshold": 6},
    "topics": [
        {"id": "tenure", "name": "Mineral tenure", "description": "Changes to staking and claims.",
         "include": ["Staking bans", "Mine-site power: diesel"], "keywords": ["staking", "claims", "MTO"]},
        {"id": "ports", "name": "Ports & rail", "description": "Ports and railways.", "keywords": ["port", "CN"]},
    ],
}


def rss(entries, host="news.test"):
    body = "".join(f"<item><title>{t}</title><link>https://{host}/{n}</link><guid>g{n}</guid>"
                   f"<description>{d}</description></item>" for n, (t, d) in enumerate(entries))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>N</title>{body}</channel></rss>'


class FakeClaude:
    """An Anthropic Messages API endpoint that scores by keyword, like a very literal model."""

    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "overloaded"}})
        items = json.loads(body["messages"][0]["content"].split("Items:\n", 1)[1])
        results = []
        for it in items:
            scores = []
            if "staking" in it["title"].lower():
                scores.append({"topic": "tenure", "score": 9})
            if "port" in it["title"].lower():
                scores.append({"topic": "ports", "score": 4})
            r = {"id": it["id"], "scores": scores, "reason": f"About {it['title'][:20]}"}
            if it.get("wants_summary"):
                r["summary"] = "This order does a thing."
            results.append(r)
        return httpx.Response(200, json={
            "content": [{"type": "tool_use", "name": body["tool_choice"]["name"], "input": {"results": results}}],
            "usage": {"input_tokens": 2000, "output_tokens": 300},
        })

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def setup(config_dir, llm=None):
    (config_dir / "topics.yaml").write_text(yaml.safe_dump(TOPICS))
    cfg = yaml.safe_load((config_dir / "config.yaml").read_text())
    cfg["llm"] = {"backend": "anthropic", "base_url": "http://pi.test:8082", **(llm or {})}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(cfg))


def rows(data_dir):
    conn = db.connect(db.db_path(data_dir))
    out = {}
    for r in conn.execute("SELECT * FROM items"):
        sc = {s["topic_id"]: s["score"] for s in conn.execute(
            "SELECT topic_id, score FROM scores WHERE item_id = ? AND stage IN ('llm', 'keywords')", (r["id"],))}
        out[r["title"]] = (r["screen_state"], sc, r["screen_reason"], r["screen_summary"])
    return out


def test_prefilter_scoring_and_mutes():
    t = Topic(id="tenure", keywords=["staking", "mineral tenure", "MTO"])
    assert prefilter.score([t], "New staking ban announced", "") == {"tenure": 0.3}
    assert prefilter.score([t], "Update", "mineral-tenure changes and staking rules") == {"tenure": 0.2}
    assert prefilter.score([t], "Photo of the day", "visit https://x.test/mto-photos") == {}  # MTO needs capitals
    assert prefilter.muted_by(["sponsored", "/^Podcast:/"], "Podcast: weekly wrap", "") == "/^Podcast:/"


def test_screening_flow_through_claude_format(site, dirs, monkeypatch):
    config_dir, data_dir = dirs
    setup(config_dir)
    site.add("https://news.test/rss", rss([
        ("Yukon pauses staking near Dawson", "A staking pause affects claims."),
        ("Prince Rupert port expansion approved", "The port will grow."),
        ("Local bake sale raises funds", "Cookies."),
        ("Sponsored: buy this stock", "staking staking claims"),
    ]), "application/rss+xml")
    write_sources(config_dir, "s.yaml", [
        {"id": "news", "feed_url": "https://news.test/rss", "full_text": "never",
         "filter": {"mute": ["Sponsored"]}},
    ])
    fake = FakeClaude()
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport, llm_transport=fake.transport).run_once())
    r = rows(data_dir)
    assert r["Yukon pauses staking near Dawson"][:2] == ("screened", {"tenure": 9.0})
    assert r["Prince Rupert port expansion approved"][1] == {"ports": 4.0}
    assert r["Local bake sale raises funds"][0] == "dropped"  # no keywords: never cost a call
    assert r["Sponsored: buy this stock"][0] == "muted"
    assert len(fake.calls) == 1 and fake.calls[0]["tool_choice"]["type"] == "tool"
    assert "Mine-site power: diesel" in fake.calls[0]["system"]
    conn = db.connect(db.db_path(data_dir))
    call = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert call["items"] == 2 and call["cost_usd"] == pytest.approx(2000 * 0.25e-6 + 300 * 1.5e-6)


def test_modes_cache_and_summary(site, dirs):
    config_dir, data_dir = dirs
    setup(config_dir)
    site.add("https://a.test/rss", rss(host="a.test", entries=[("Staking rules change", "MTO update")]), "application/rss+xml")
    site.add("https://b.test/rss", rss(host="b.test", entries=[("Weekend weather", "Sunny.")]), "application/rss+xml")
    site.add("https://c.test/rss", rss(host="c.test", entries=[("Port strike looms at CN terminal", "port")]), "application/rss+xml")
    site.add("https://d.test/rss", rss(host="d.test", entries=[("OIC 2026/14 staking order", "An order under the Quartz Mining Act.")]),
             "application/rss+xml")
    write_sources(config_dir, "s.yaml", [
        {"id": "a", "feed_url": "https://a.test/rss", "full_text": "never"},
        {"id": "b", "feed_url": "https://b.test/rss", "full_text": "never", "filter": {"mode": "none"}},
        {"id": "c", "feed_url": "https://c.test/rss", "full_text": "never", "filter": {"mode": "keywords"}},
        {"id": "d", "feed_url": "https://d.test/rss", "full_text": "never",
         "filter": {"mode": "llm", "summarize": True, "topics": ["tenure"]}},
    ])
    fake = FakeClaude()
    w = Worker(config_dir, data_dir, transport=site.transport, llm_transport=fake.transport)
    asyncio.run(w.run_once())
    r = rows(data_dir)
    assert r["Weekend weather"][0] == "passthrough"
    assert r["Port strike looms at CN terminal"][:2] == ("screened", {"ports": 10.0})  # keywords, no LLM
    assert r["OIC 2026/14 staking order"][3] == "This order does a thing."
    assert len(fake.calls) == 2  # grouped by topic set: [tenure, ports] and [tenure]+summary
    d_call = next(c for c in fake.calls if "wants_summary" in c["messages"][0]["content"])
    assert "ports:" not in d_call["system"]  # only the source's own topics are sent

    # re-screening the same content is free
    from readwell.screen.run import reset_for_rescreen
    conn = db.connect(db.db_path(data_dir))
    assert reset_for_rescreen(conn, 7) == 4
    asyncio.run(w.run_once([]))
    assert len(fake.calls) == 2
    assert rows(data_dir)["Staking rules change"][:2] == ("screened", {"tenure": 9.0})


def test_budget_defers_and_errors_back_off(site, dirs):
    from readwell.screen import run as screen_run

    config_dir, data_dir = dirs
    setup(config_dir, {"daily_budget_usd": 0.0001})
    site.add("https://news.test/rss", rss([("Staking news", "claims")]), "application/rss+xml")
    write_sources(config_dir, "s.yaml", [{"id": "news", "feed_url": "https://news.test/rss", "full_text": "never"}])
    fake = FakeClaude()
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport, llm_transport=fake.transport).run_once())
    state, _, reason, _ = rows(data_dir)["Staking news"]
    assert state == "deferred" and "budget" in reason and not fake.calls

    setup(config_dir)  # budget back to normal, but the LLM is down
    down = FakeClaude(status=529)
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport, llm_transport=down.transport).run_once())
    state, _, reason, _ = rows(data_dir)["Staking news"]
    assert state == "deferred" and "529" in reason
    assert screen_run._backoff_until > 0
    screen_run._backoff_until = 0


def test_missing_key_is_reported(site, dirs, monkeypatch):
    config_dir, data_dir = dirs
    setup(config_dir, {"base_url": None})
    monkeypatch.delenv("READWELL_LLM_KEY", raising=False)
    site.add("https://news.test/rss", rss([("Staking news", "claims")]), "application/rss+xml")
    write_sources(config_dir, "s.yaml", [{"id": "news", "feed_url": "https://news.test/rss", "full_text": "never"}])
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport, llm_transport=FakeClaude().transport).run_once())
    state, _, reason, _ = rows(data_dir)["Staking news"]
    assert state == "deferred" and "READWELL_LLM_KEY" in reason


def test_gemini_backend_schema_thinking_fallback_and_usage():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Thinking level is not supported"}})
        assert body["generationConfig"]["responseSchema"]["type"] == "OBJECT"
        out = {"results": [{"id": "i1", "scores": [{"topic": "tenure", "score": 7}], "reason": "Staking"}]}
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(out)}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5},
        })

    s = GeminiScreener(LlmCfg(backend="gemini", base_url="http://g.test/v1beta"), httpx.MockTransport(handler))
    res, usage = asyncio.run(s.screen([ScreenInput("42", "src", "Staking", None, "text")],
                                      [Topic(id="tenure")], []))
    assert res[0].key == "42" and res[0].scores == {"tenure": 7.0}
    assert usage.output_tokens == 25 and len(seen) == 2


def test_openai_backend_for_grok_and_friends():
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert request.headers["authorization"] == "Bearer k"
        out = {"results": [{"id": "i1", "scores": [], "reason": "Unrelated"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    import os
    os.environ["XAI_KEY"] = "k"
    s = OpenAIScreener(LlmCfg(backend="openai", base_url="https://api.x.test/v1", model="grok-x",
                              api_key_env="XAI_KEY"), httpx.MockTransport(handler))
    res, usage = asyncio.run(s.screen([ScreenInput("1", "s", "t", None, "x")], [Topic(id="tenure")], []))
    assert res[0].scores == {} and usage.input_tokens == 10


def test_prompt_carries_feedback_examples():
    from readwell.screen.llm import Example

    system, user = build_prompt([ScreenInput("1", "CBC", "Title", "2026-11-26T00:00:00Z", "Body", note="trade press")],
                                [Topic(id="tenure", description="Claims.")], ["Yukon"],
                                {"tenure": [Example(1, "Staking ban", "Yukon News"), Example(-1, "Gold price", "X")]})
    assert 'relevant: "Staking ban" (Yukon News)' in system and 'NOT relevant: "Gold price"' in system
    assert '"source_note": "trade press"' in user and '"published": "2026-11-26"' in user
