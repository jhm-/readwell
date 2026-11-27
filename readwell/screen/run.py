"""The screening stage: decide which topic feeds each new item belongs in.

For every enriched cluster lead that hasn't been screened:

1. source ``mute`` terms → ``muted``
2. filter mode ``none`` → ``passthrough`` (only its views and all.atom)
3. keyword prefilter, stored as ``prefilter`` scores
4. mode ``keywords`` → topic matches straight from the prefilter
5. mode ``topics`` with nothing above the floor → ``dropped``
6. otherwise the LLM, in batches grouped by topic set, through the cache and under the budget.
   Over budget → ``deferred``; deferred items are retried when the budget allows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Config, SourceCfg
from ..db import tx
from ..timeutil import iso_ago, now_iso
from . import prefilter
from .llm import Example, LlmError, Screener, ScreenInput, ScreenResult, Usage, build_prompt, estimate_tokens
from .topics import Topic

log = logging.getLogger(__name__)

_backoff_until = 0.0  # monotonic; after an LLM failure, wait before trying again


@dataclass
class ScreenStats:
    checked: int = 0
    muted: int = 0
    passthrough: int = 0
    dropped: int = 0
    keywords: int = 0
    cached: int = 0
    llm_items: int = 0
    llm_calls: int = 0
    deferred: int = 0
    errors: int = 0
    cost_usd: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.checked)


# --------------------------------------------------------------------------- budget


def budget_day(cfg: Config) -> str:
    try:
        tz = ZoneInfo(cfg.settings.llm.budget_tz)
    except Exception:  # noqa: BLE001 — bad tz name: fall back to UTC rather than stop screening
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


def spent_today(conn: sqlite3.Connection, cfg: Config) -> float:
    r = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE day = ?", (budget_day(cfg),)).fetchone()
    return float(r[0])


def cost(cfg: Config, u: Usage) -> float:
    llm = cfg.settings.llm
    return u.input_tokens * llm.price_in_per_m / 1e6 + u.output_tokens * llm.price_out_per_m / 1e6


def _record_call(conn: sqlite3.Connection, cfg: Config, n_items: int, u: Usage, error: str | None) -> float:
    c = cost(cfg, u)
    with tx(conn):
        conn.execute(
            "INSERT INTO llm_calls (at, day, model, purpose, items, input_tokens, output_tokens, cost_usd, error)"
            " VALUES (?, ?, ?, 'screen', ?, ?, ?, ?, ?)",
            (now_iso(), budget_day(cfg), cfg.settings.llm.model, n_items, u.input_tokens, u.output_tokens, c, error),
        )
    return c


# --------------------------------------------------------------------------- helpers


def source_topics(cfg: Config, s: SourceCfg) -> list[Topic]:
    return cfg.topics.subset(s.filter.topics) if cfg.topics else []


def wants_summary(cfg: Config, s: SourceCfg, doc_type: str) -> bool:
    if s.filter.summarize is not None:
        return s.filter.summarize
    return doc_type in cfg.settings.screen.summarize_doc_types


def _text(r: sqlite3.Row) -> str:
    if r["content_text"]:
        return r["content_text"]
    from ..pipeline.normalize import html_to_text

    return html_to_text(r["content_html"] or r["summary"] or "")


def _clip(text: str, words: int) -> str:
    w = text.split()
    return " ".join(w[:words]) + (" …" if len(w) > words else "")


def _content_key(r: sqlite3.Row, text: str) -> str:
    return r["content_hash"] or hashlib.sha1(f"{r['title']}\n{text}".lower().encode()).hexdigest()


def _set_state(conn: sqlite3.Connection, item_id: int, state: str, reason: str | None = None) -> None:
    conn.execute("UPDATE items SET screen_state = ?, screen_reason = ?, screened_at = ? WHERE id = ?",
                 (state, reason, now_iso(), item_id))


def _write_scores(conn: sqlite3.Connection, item_id: int, stage: str, scores: dict[str, float]) -> None:
    conn.execute("DELETE FROM scores WHERE item_id = ? AND stage = ?", (item_id, stage))
    at = now_iso()
    conn.executemany("INSERT INTO scores (item_id, topic_id, stage, score, at) VALUES (?, ?, ?, ?, ?)",
                     [(item_id, t, stage, v, at) for t, v in scores.items()])


def examples(conn: sqlite3.Connection, cfg: Config, topic_ids: list[str]) -> dict[str, list[Example]]:
    n = cfg.settings.screen.feedback_examples
    if n <= 0:
        return {}
    out: dict[str, list[Example]] = defaultdict(list)
    for tid in topic_ids:
        for vote in (1, -1):
            for r in conn.execute(
                "SELECT title, source_id FROM feedback WHERE topic_id = ? AND vote = ? AND title IS NOT NULL"
                " ORDER BY at DESC LIMIT ?", (tid, vote, n),
            ):
                s = cfg.sources.get(r["source_id"])
                out[tid].append(Example(vote, r["title"], s.display_name if s else r["source_id"]))
    return dict(out)


# --------------------------------------------------------------------------- the stage


@dataclass
class _Job:
    row: sqlite3.Row
    source: SourceCfg
    topics: list[Topic]
    summarize: bool
    text: str
    key: str       # content key for the cache
    version: str   # topic-set version (+ summary flag)


def _pending(conn: sqlite3.Connection, cfg: Config, limit: int, item_ids: list[int] | None,
             max_age_days: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT i.* FROM items i JOIN sources s ON s.id = i.source_id"
           " WHERE s.in_config = 1 AND i.is_lead = 1 AND i.state = 'enriched'"
           " AND (i.screen_state IS NULL OR i.screen_state = 'deferred')")
    params: list[object] = []
    if item_ids:
        sql += f" AND i.id IN ({','.join('?' * len(item_ids))})"
        params += item_ids
    else:
        sql += " AND i.first_seen_at >= ?"
        params.append(iso_ago(days=max_age_days or cfg.settings.screen.max_age_days))
    sql += " ORDER BY i.first_seen_at DESC, i.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


async def screen_pending(conn: sqlite3.Connection, cfg: Config, screener: Screener | None, *,
                         max_batches: int | None = None, item_ids: list[int] | None = None,
                         max_age_days: int | None = None) -> ScreenStats:
    global _backoff_until
    st = ScreenStats()
    llm = cfg.settings.llm
    sc = cfg.settings.screen
    rows = _pending(conn, cfg, 5000, item_ids, max_age_days)
    if not rows:
        return st
    jobs: list[_Job] = []
    with tx(conn):
        for r in rows:
            st.checked += 1
            s = cfg.sources.get(r["source_id"])
            if s is None or r["doc_type"] == "removal":
                _set_state(conn, r["id"], "skipped")
                continue
            text = _text(r)
            title = r["title"] or ""
            hit = prefilter.muted_by(s.filter.mute, title, text)
            if hit:
                _set_state(conn, r["id"], "muted", f"muted: {hit}")
                st.muted += 1
                continue
            if s.filter.mode == "none":
                _set_state(conn, r["id"], "passthrough")
                st.passthrough += 1
                continue
            topics = source_topics(cfg, s)
            if not topics:
                _set_state(conn, r["id"], "skipped", "no topics to screen against")
                continue
            kw = prefilter.score(topics, title, text, s.filter.keywords_add)
            _write_scores(conn, r["id"], "prefilter", kw)
            if s.filter.mode == "keywords":
                hits = {t: 10.0 for t, v in kw.items() if v >= sc.keyword_threshold}
                _write_scores(conn, r["id"], "keywords", hits)
                reason = ("keywords: " + ", ".join(cfg.topics.by_id[t].label for t in hits)) if hits else None
                _set_state(conn, r["id"], "screened", reason)
                st.keywords += 1
                continue
            if s.filter.mode == "topics" and max(kw.values(), default=0) < sc.prefilter_floor:
                _set_state(conn, r["id"], "dropped", "no topic keywords")
                st.dropped += 1
                continue
            summarize = wants_summary(cfg, s, r["doc_type"])
            ids = [t.id for t in topics]
            version = cfg.topics.version(ids) + (":s" if summarize else "")
            words = llm.doc_max_words if r["doc_type"] != "article" else llm.max_words
            jobs.append(_Job(r, s, topics, summarize, _clip(text, words), _content_key(r, text), version))

    if not jobs:
        return st

    # cache hits cost nothing
    todo: list[_Job] = []
    with tx(conn):
        for j in jobs:
            c = conn.execute("SELECT result FROM screen_cache WHERE content_hash = ? AND version = ?",
                             (j.key, j.version)).fetchone()
            if c:
                d = json.loads(c["result"])
                _apply(conn, j, ScreenResult(str(j.row["id"]), d.get("scores") or {}, d.get("reason") or "",
                                             d.get("summary")))
                st.cached += 1
            else:
                todo.append(j)
    if not todo:
        return st

    def defer(js: list[_Job], why: str) -> None:
        with tx(conn):
            for j in js:
                if j.row["screen_state"] != "deferred" or (j.row["screen_reason"] or "") != why:
                    _set_state(conn, j.row["id"], "deferred", why)
        st.deferred += len(js)

    if llm.backend == "none" or screener is None:
        defer(todo, "LLM screening is turned off")
        return st
    if not screener.configured:
        defer(todo, f"no LLM key: set {llm.api_key_env} in /etc/readwell/env, or point llm.base_url at a proxy")
        return st
    if time.monotonic() < _backoff_until:
        return st  # recent failure; try again later (items stay as they are)

    groups: dict[tuple[tuple[str, ...], bool], list[_Job]] = defaultdict(list)
    for j in todo:
        groups[(tuple(t.id for t in j.topics), j.summarize)].append(j)
    batches = [g[i:i + llm.batch_size] for g in groups.values() for i in range(0, len(g), llm.batch_size)]
    limit = max_batches if max_batches is not None else llm.max_batches_per_tick

    for n, batch in enumerate(batches):
        if n >= limit:
            break
        topics = batch[0].topics
        ids = [t.id for t in topics]
        inputs = [ScreenInput(str(j.row["id"]), j.source.display_name, j.row["title"] or "(untitled)",
                              j.row["published_at"], j.text, j.source.filter.note, j.summarize) for j in batch]
        ex = examples(conn, cfg, ids)
        est_in, est_out = estimate_tokens(*build_prompt(inputs, topics, cfg.topics.jurisdictions, ex), len(batch))
        if spent_today(conn, cfg) + cost(cfg, Usage(est_in, est_out)) > llm.daily_budget_usd:
            rest = [j for b in batches[n:] for j in b]
            defer(rest, f"daily LLM budget (${llm.daily_budget_usd:.2f}) reached; will retry")
            log.warning("LLM budget reached: %d item(s) deferred", len(rest))
            break
        try:
            results, usage = await screener.screen(inputs, topics, cfg.topics.jurisdictions, ex)
        except LlmError as e:
            st.errors += 1
            st.cost_usd += _record_call(conn, cfg, len(batch), e.usage, str(e)[:500])
            _backoff_until = time.monotonic() + (300 if e.retryable else 1800)
            defer([j for b in batches[n:] for j in b], f"LLM error, will retry: {str(e)[:160]}")
            log.warning("LLM screening failed: %s", e)
            break
        st.llm_calls += 1
        st.cost_usd += _record_call(conn, cfg, len(batch), usage, None)
        by_key = {r.key: r for r in results}
        with tx(conn):
            for j in batch:
                res = by_key.get(str(j.row["id"]))
                if res is None:  # the model skipped it; try again next tick
                    continue
                _apply(conn, j, res)
                conn.execute("INSERT OR REPLACE INTO screen_cache (content_hash, version, result, at)"
                             " VALUES (?, ?, ?, ?)",
                             (j.key, j.version, json.dumps({"scores": res.scores, "reason": res.reason,
                                                            "summary": res.summary}), now_iso()))
                st.llm_items += 1
    return st


def _apply(conn: sqlite3.Connection, j: _Job, res: ScreenResult) -> None:
    _write_scores(conn, j.row["id"], "llm", {t: float(v) for t, v in res.scores.items()})
    conn.execute(
        "UPDATE items SET screen_state = 'screened', screen_reason = ?, screen_summary = ?, screen_version = ?,"
        " screened_at = ? WHERE id = ?",
        (res.reason or None, res.summary, j.version, now_iso(), j.row["id"]),
    )


def reset_for_rescreen(conn: sqlite3.Connection, days: int, source_ids: list[str] | None = None) -> int:
    """Mark recent screened/deferred/dropped items for screening again (e.g. after editing topics.yaml)."""
    sql = ("UPDATE items SET screen_state = NULL WHERE is_lead = 1 AND state = 'enriched'"
           " AND screen_state IN ('screened', 'deferred', 'dropped', 'skipped', 'passthrough', 'muted')"
           " AND first_seen_at >= ?")
    params: list[object] = [iso_ago(days=days)]
    if source_ids:
        sql += f" AND source_id IN ({','.join('?' * len(source_ids))})"
        params += source_ids
    with tx(conn):
        return conn.execute(sql, params).rowcount
