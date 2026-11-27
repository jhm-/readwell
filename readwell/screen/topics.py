"""Topic profiles (topics.yaml) and combined output feeds (views.yaml).

topics.yaml::

    defaults:
      jurisdictions: [Yukon, British Columbia]   # soft weighting given to the LLM
      threshold: 6                               # score (0-10) needed to land in a topic's feed
    topics:
      - id: land-access-tenure
        name: Mineral tenure & land access
        description: >
          Anything that changes where and how mineral rights can be acquired or held.
        include: [Staking bans and land withdrawals, New parks and protected areas]
        exclude: [Urban zoning]
        keywords: [staking, withdrawal, protected area]   # prefilter only
        threshold: 7                                       # optional, per topic

views.yaml::

    views:
      main:
        name: Main
        topics: all          # every topic match ...
        min_score: 6         # ... at or above this score (default: each topic's own threshold)
        tags: [general-interest]   # ... plus every item from sources with these tags
        sources: []          # ... plus every item from these sources
        exclude_tags: [registry]   # ... but nothing from sources with these tags
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROMPT_REV = "1"  # bump when the prompt template changes in a way that should re-screen items


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str | None = None
    description: str = ""
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    threshold: float | None = None

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", v):
            raise ValueError("topic id must be lowercase letters, digits, '-' or '_'")
        return v

    @field_validator("include", "exclude", "keywords", mode="before")
    @classmethod
    def _plain_lines(cls, v: Any) -> Any:
        # "- Mine-site power: diesel, LNG" is a one-key mapping in YAML; take it as the line it looks like
        if isinstance(v, list):
            return [", ".join(f"{k}: {val}" for k, val in e.items()) if isinstance(e, dict) else e for e in v]
        return v

    @property
    def label(self) -> str:
        return self.name or self.id


class _Defaults(BaseModel):
    model_config = ConfigDict(extra="ignore")
    jurisdictions: list[str] = Field(default_factory=list)
    threshold: float = 6


@dataclass
class TopicSet:
    topics: list[Topic] = field(default_factory=list)
    jurisdictions: list[str] = field(default_factory=list)
    default_threshold: float = 6

    @property
    def by_id(self) -> dict[str, Topic]:
        return {t.id: t for t in self.topics}

    def threshold(self, topic_id: str) -> float:
        t = self.by_id.get(topic_id)
        return t.threshold if t and t.threshold is not None else self.default_threshold

    def subset(self, ids: Literal["all"] | list[str]) -> list[Topic]:
        if ids == "all":
            return list(self.topics)
        wanted = set(ids)
        return [t for t in self.topics if t.id in wanted]

    def version(self, topic_ids: list[str]) -> str:
        """Changes whenever anything the LLM sees for these topics changes; part of the cache key."""
        sel = [t for t in self.topics if t.id in set(topic_ids)]
        blob = json.dumps({
            "rev": PROMPT_REV,
            "j": self.jurisdictions,
            "t": [[t.id, t.label, t.description, t.include, t.exclude] for t in sel],
        }, sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]


def parse_topics(raw: Any, errors: list[str]) -> TopicSet:
    if not raw:
        return TopicSet()
    if not isinstance(raw, dict):
        errors.append("topics.yaml: expected a mapping with 'topics' (and optional 'defaults')")
        return TopicSet()
    try:
        d = _Defaults.model_validate(raw.get("defaults") or {})
    except ValidationError as e:
        errors.append(f"topics.yaml defaults: {e.errors()[0]['msg']}")
        d = _Defaults()
    out: list[Topic] = []
    seen: set[str] = set()
    for n, entry in enumerate(raw.get("topics") or [], 1):
        try:
            t = Topic.model_validate(entry)
        except ValidationError as e:
            ident = entry.get("id", f"#{n}") if isinstance(entry, dict) else f"#{n}"
            errors.append(f"topics.yaml [{ident}]: {e.errors()[0]['loc'][0]}: {e.errors()[0]['msg']}")
            continue
        if t.id in seen:
            errors.append(f"topics.yaml [{t.id}]: duplicate id; skipped")
            continue
        seen.add(t.id)
        out.append(t)
    return TopicSet(out, d.jurisdictions, d.threshold)


class View(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    name: str | None = None
    topics: Literal["all"] | list[str] = Field(default_factory=list)
    min_score: float | None = None
    tags: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)  # leave out sources with these tags

    @property
    def label(self) -> str:
        return self.name or self.id


def parse_views(raw: Any, topics: TopicSet, errors: list[str]) -> dict[str, View]:
    if not raw:
        return {}
    body = raw.get("views") if isinstance(raw, dict) else None
    if not isinstance(body, dict):
        errors.append("views.yaml: expected 'views:' followed by one mapping per view")
        return {}
    out: dict[str, View] = {}
    for vid, entry in body.items():
        if not isinstance(vid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", vid):
            errors.append(f"views.yaml: view id {vid!r} must be lowercase letters, digits, '-' or '_'")
            continue
        try:
            v = View.model_validate({**(entry or {}), "id": vid})
        except ValidationError as e:
            errors.append(f"views.yaml [{vid}]: {e.errors()[0]['loc'][0]}: {e.errors()[0]['msg']}")
            continue
        if isinstance(v.topics, list):
            unknown = [t for t in v.topics if t not in topics.by_id]
            if unknown:
                errors.append(f"views.yaml [{vid}]: unknown topic(s) {', '.join(unknown)}")
        out[vid] = v
    return out
