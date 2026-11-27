"""Plain data passed between fetchers and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawItem:
    """One item as a source module found it, before normalization."""

    guid: str | None
    url: str | None
    title: str | None
    author: str | None = None
    published_at: str | None = None  # ISO UTC
    summary_html: str | None = None
    content_html: str | None = None
    doc_type: str = "article"


@dataclass
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    not_modified: bool = False
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    resolved_url: str | None = None  # set when discovery found (or re-found) the feed
    feed_title: str | None = None
    site_url: str | None = None


class SourceError(Exception):
    """A fetch that failed in a way worth reporting in health.atom."""

    kind = "error"

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retry_after_s: float | None = None,
        clear_resolved: bool = False,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        self.clear_resolved = clear_resolved  # forget the discovered feed URL and rediscover next run


class DiscoveryError(SourceError):
    kind = "discovery"


class HttpError(SourceError):
    kind = "http"


class NetworkError(SourceError):
    kind = "network"


class ParseError(SourceError):
    kind = "parse"
