"""Which feeds an item belongs in, worked out from its scores, your votes and the config.

Rules (kept here so feeds, the status page and the CLI agree):

- An item is in topic T when its ``llm`` or ``keywords`` score for T is at or above the
  threshold: the source's ``filter.threshold`` if set, else T's own, else the default.
- A 👍 for T puts it in T whatever the score; a 👎 takes it out.
- A view collects its topics' matches (``min_score`` overrides the thresholds), plus every
  item from its ``sources``, from sources carrying one of its ``tags``, and from sources that
  name it in ``deliver_to``.
- ``max_per_day`` on a source keeps only its N best-scoring matches per day in topic feeds.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import Config
from ..screen.topics import View


@dataclass
class ItemScores:
    llm: dict[str, float] = field(default_factory=dict)       # llm + keywords stages
    votes: dict[str, int] = field(default_factory=dict)


def load_scores(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, ItemScores]:
    out: dict[int, ItemScores] = defaultdict(ItemScores)
    if not item_ids:
        return out
    marks = ",".join("?" * len(item_ids))
    for r in conn.execute(f"SELECT item_id, topic_id, stage, score FROM scores WHERE item_id IN ({marks})"
                          " AND stage IN ('llm', 'keywords')", item_ids):
        cur = out[r["item_id"]].llm
        cur[r["topic_id"]] = max(cur.get(r["topic_id"], 0), r["score"])
    for r in conn.execute(f"SELECT item_id, topic_id, vote FROM feedback WHERE item_id IN ({marks})", item_ids):
        out[r["item_id"]].votes[r["topic_id"]] = r["vote"]
    return out


def threshold(cfg: Config, source_id: str, topic_id: str) -> float:
    s = cfg.sources.get(source_id)
    if s is not None and s.filter.threshold is not None:
        return s.filter.threshold
    return cfg.topics.threshold(topic_id) if cfg.topics else 6


def matches(cfg: Config, source_id: str, sc: ItemScores, *, min_score: float | None = None) -> dict[str, float]:
    """Topic id → score for every topic this item is in (a 👍 without a score counts as 10)."""
    known = cfg.topics.by_id if cfg.topics else {}
    out: dict[str, float] = {}
    for tid, score in sc.llm.items():
        if tid not in known or sc.votes.get(tid) == -1:
            continue
        need = min_score if min_score is not None else threshold(cfg, source_id, tid)
        if score >= need:
            out[tid] = score
    for tid, vote in sc.votes.items():
        if vote == 1 and tid in known:
            out.setdefault(tid, sc.llm.get(tid, 10.0))
    return out


def view_sources(cfg: Config, v: View) -> set[str]:
    ids = set(v.sources)
    for s in cfg.sources.values():
        if v.id in s.deliver_to or (v.tags and set(v.tags) & set(s.tags)):
            ids.add(s.id)
    return ids


def view_topics(cfg: Config, v: View) -> set[str]:
    if not cfg.topics:
        return set()
    return {t.id for t in cfg.topics.subset(v.topics)} if v.topics else set()
