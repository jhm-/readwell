"""Configuration: settings, sources, topics and views, loaded from a config directory.

Layout of ``READWELL_CONFIG_DIR``::

    config.yaml          # settings (optional; defaults below)
    topics.yaml          # topic profiles (used from Phase 3)
    views.yaml           # combined output feeds (used from Phase 3)
    sources/**/*.yaml    # source definitions

A source file may hold one source (a mapping with ``id``), a plain list of
sources, or a mapping with ``defaults`` and ``sources`` keys.

The config directory may live on a network share. After every successful load
a copy is kept in ``<data_dir>/config-cache``; if the share is unavailable the
cached copy is used instead, so a NAS outage doesn't stop Readwell.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

SOURCE_KINDS = {"feed", "listing", "numbered", "documents", "email"}
IMPLEMENTED_KINDS = {"feed"}


# --------------------------------------------------------------------------- settings


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerCfg(_Strict):
    host: str = "0.0.0.0"
    port: int = 8080
    base_url: str = "http://localhost:8080"


class FetchCfg(_Strict):
    user_agent: str = "Readwell/0.1 (+https://github.com/jhm-/readwell; personal feed reader)"
    timeout_s: float = 30.0
    max_concurrency: int = 8
    per_host_delay_s: float = 1.0
    max_backoff_hours: float = 24.0
    max_bytes: int = 15_000_000


class WorkerCfg(_Strict):
    tick_s: int = 30
    default_interval_min: int = 60


class FeedsCfg(_Strict):
    title: str = "Readwell"
    max_entries: int = 200
    max_age_days: int = 30


class HealthCfg(_Strict):
    failure_threshold: int = 5


class DedupeCfg(_Strict):
    url_window_days: int = 14
    title_window_hours: int = 72
    min_title_words: int = 5
    title_containment: float = 0.9


class RetentionCfg(_Strict):
    content_days: int = 90
    backups_keep: int = 14


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")  # later phases add sections (llm, exchange, ...)

    server: ServerCfg = Field(default_factory=ServerCfg)
    fetch: FetchCfg = Field(default_factory=FetchCfg)
    worker: WorkerCfg = Field(default_factory=WorkerCfg)
    feeds: FeedsCfg = Field(default_factory=FeedsCfg)
    health: HealthCfg = Field(default_factory=HealthCfg)
    dedupe: DedupeCfg = Field(default_factory=DedupeCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)


# --------------------------------------------------------------------------- sources


class FilterCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: Literal["topics", "llm", "keywords", "none"] = "topics"


class SourceCfg(BaseModel):
    model_config = ConfigDict(extra="allow")  # kind-specific keys (recipes, templates, ...)

    id: str
    kind: str = "feed"
    url: str | None = None
    feed_url: str | None = None  # skip discovery when set
    name: str | None = None
    enabled: bool = True
    interval_min: int = 60
    tags: list[str] = Field(default_factory=list)
    filter: FilterCfg = Field(default_factory=FilterCfg)
    deliver_to: list[str] = Field(default_factory=list)
    previous_ids: list[str] = Field(default_factory=list)  # rename: carry history over from these ids
    note: str | None = None
    check: bool = False

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", v):
            raise ValueError("id must be lowercase letters, digits, '-' or '_' (max 64 chars)")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in SOURCE_KINDS:
            raise ValueError(f"unknown kind {v!r}; expected one of {sorted(SOURCE_KINDS)}")
        return v

    @field_validator("interval_min")
    @classmethod
    def _interval(cls, v: int) -> int:
        if v < 5:
            raise ValueError("interval_min must be at least 5")
        return v

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def implemented(self) -> bool:
        return self.kind in IMPLEMENTED_KINDS


# --------------------------------------------------------------------------- loading


@dataclass
class Config:
    settings: Settings
    sources: dict[str, SourceCfg]
    config_dir: Path
    fingerprint: str
    from_cache: bool = False
    errors: list[str] = field(default_factory=list)
    raw_topics: Any = None
    raw_views: Any = None


class ConfigError(Exception):
    pass


def default_config_dir() -> Path:
    return Path(os.environ.get("READWELL_CONFIG_DIR", "./config")).expanduser()


def default_data_dir() -> Path:
    return Path(os.environ.get("READWELL_DATA_DIR", "./data")).expanduser()


def _yaml_files(config_dir: Path) -> list[Path]:
    files = [config_dir / n for n in ("config.yaml", "topics.yaml", "views.yaml")]
    files = [f for f in files if f.is_file()]
    src = config_dir / "sources"
    if src.is_dir():
        files += sorted(p for p in src.rglob("*.yaml") if p.is_file())
        files += sorted(p for p in src.rglob("*.yml") if p.is_file())
    return files


def fingerprint(config_dir: Path) -> str:
    """Cheap change detector: paths, sizes and mtimes of every config file."""
    h = hashlib.sha256()
    try:
        for f in _yaml_files(config_dir):
            st = f.stat()
            h.update(f"{f.relative_to(config_dir)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    except OSError as e:
        h.update(f"error:{e}".encode())
    return h.hexdigest()


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _source_entries(doc: Any) -> list[dict[str, Any]]:
    if doc is None:
        return []
    if isinstance(doc, list):
        return [dict(e) for e in doc]
    if isinstance(doc, dict) and "sources" in doc:
        defaults = doc.get("defaults") or {}
        return [{**defaults, **(e or {})} for e in (doc.get("sources") or [])]
    if isinstance(doc, dict) and "id" in doc:
        return [doc]
    raise ConfigError("expected a source mapping with 'id', a list of sources, or {defaults, sources}")


def _usable(config_dir: Path) -> bool:
    try:
        return config_dir.is_dir() and os.access(config_dir, os.R_OK | os.X_OK)
    except OSError:
        return False


def load_config(config_dir: Path | None = None, data_dir: Path | None = None) -> Config:
    config_dir = (config_dir or default_config_dir()).resolve()
    data_dir = data_dir or default_data_dir()
    cache_dir = data_dir / "config-cache"

    from_cache = False
    if not _usable(config_dir):
        if _usable(cache_dir):
            log.warning("config dir %s unavailable; using cached copy in %s", config_dir, cache_dir)
            config_dir, from_cache = cache_dir, True
        else:
            raise ConfigError(f"config dir {config_dir} is not readable and no cached copy exists")

    errors: list[str] = []
    fp = fingerprint(config_dir)

    cfg_file = config_dir / "config.yaml"
    try:
        settings = Settings.model_validate(_read_yaml(cfg_file) or {}) if cfg_file.is_file() else Settings()
    except (yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"{cfg_file}: {e}") from e

    sources: dict[str, SourceCfg] = {}
    src_dir = config_dir / "sources"
    files = sorted(src_dir.rglob("*.y*ml")) if src_dir.is_dir() else []
    for path in files:
        rel = path.relative_to(config_dir)
        try:
            entries = _source_entries(_read_yaml(path))
        except (yaml.YAMLError, ConfigError) as e:
            errors.append(f"{rel}: {e}")
            continue
        for n, entry in enumerate(entries, 1):
            if isinstance(entry.get("id"), bool | int | float):
                errors.append(f"{rel} [entry #{n}]: id {entry['id']!r} isn't text — YAML reads bare words like "
                              "on/off/yes/no and numbers as values; put the id in quotes")
                continue
            entry.setdefault("interval_min", settings.worker.default_interval_min)
            try:
                s = SourceCfg.model_validate(entry)
            except ValidationError as e:
                ident = entry.get("id", f"entry #{n}")
                errors.append(f"{rel} [{ident}]: {e.errors()[0]['msg']}")
                continue
            if s.kind == "feed" and not (s.url or s.feed_url):
                errors.append(f"{rel} [{s.id}]: feed sources need 'url' or 'feed_url'")
                continue
            if s.id in sources:
                errors.append(f"{rel} [{s.id}]: duplicate id (already defined); skipped")
                continue
            sources[s.id] = s

    raw_topics = _safe_optional(config_dir / "topics.yaml", errors, config_dir)
    raw_views = _safe_optional(config_dir / "views.yaml", errors, config_dir)

    cfg = Config(settings, sources, config_dir, fp, from_cache, errors, raw_topics, raw_views)
    if not from_cache:
        _refresh_cache(config_dir, cache_dir)
    for e in errors:
        log.warning("config: %s", e)
    return cfg


def _safe_optional(path: Path, errors: list[str], root: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return _read_yaml(path)
    except yaml.YAMLError as e:
        errors.append(f"{path.relative_to(root)}: {e}")
        return None


def _refresh_cache(config_dir: Path, cache_dir: Path) -> None:
    """Mirror the config files into the data dir so a NAS outage isn't fatal."""
    try:
        tmp = cache_dir.with_name(cache_dir.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        for f in _yaml_files(config_dir):
            dest = tmp / f.relative_to(config_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        tmp.mkdir(parents=True, exist_ok=True)
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        tmp.rename(cache_dir)
    except OSError as e:  # the cache is a convenience; never fail a load over it
        log.warning("could not refresh config cache: %s", e)
