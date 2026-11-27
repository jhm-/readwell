"""What every source kind gets, and the registry of kinds."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..config import Config, SourceCfg
from ..fetch.http import Fetcher
from ..models import FetchResult


@dataclass
class SourceContext:
    fetcher: Fetcher
    cfg: Config
    source: SourceCfg
    row: sqlite3.Row  # the source's state row

    @property
    def cursor(self) -> dict[str, Any]:
        raw = self.row["cursor"]
        if not raw:
            return {}
        try:
            val = json.loads(raw)
        except ValueError:
            return {}
        return val if isinstance(val, dict) else {}

    @property
    def first_run(self) -> bool:
        return not self.row["baseline_done"]

    def option(self, key: str, default: Any = None) -> Any:
        return (self.source.model_extra or {}).get(key, default)


FetchFn = Callable[[SourceContext], Awaitable[FetchResult]]
REGISTRY: dict[str, FetchFn] = {}


def register(kind: str) -> Callable[[FetchFn], FetchFn]:
    def deco(fn: FetchFn) -> FetchFn:
        REGISTRY[kind] = fn
        return fn

    return deco


def dump_cursor(d: dict[str, Any]) -> str:
    return json.dumps(d, sort_keys=True)
