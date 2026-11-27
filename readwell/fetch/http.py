"""Polite HTTP fetching.

* one shared client with an honest User-Agent (never a browser or crawler disguise)
* global concurrency cap, plus one request at a time per host with a minimum gap
* conditional GET (ETag / Last-Modified) so unchanged feeds cost a 304
* size cap on response bodies
* ``Retry-After`` surfaced to the caller on 429 / 503
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from ..config import FetchCfg
from ..models import HttpError, NetworkError, TooLargeError
from ..timeutil import now

FEED_ACCEPT = (
    "application/atom+xml, application/rss+xml, application/feed+json, application/xml;q=0.9, "
    "text/xml;q=0.9, text/html;q=0.8, */*;q=0.5"
)


@dataclass
class Response:
    status: int
    url: str  # final URL after redirects
    headers: httpx.Headers
    content: bytes

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - now()).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


def is_bot_challenge(resp: Response) -> bool:
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if resp.status in (403, 429, 503) and "html" in resp.content_type:
        head = resp.content[:30000]
        return b"challenge-platform" in head or b"<title>Just a moment" in head or b"_Incapsula_Resource" in head
    return False


class Fetcher:
    def __init__(self, cfg: FetchCfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept": FEED_ACCEPT},
            timeout=httpx.Timeout(cfg.timeout_s, connect=min(15.0, cfg.timeout_s)),
            follow_redirects=True,
            max_redirects=10,
            transport=transport,
        )
        self._global = asyncio.Semaphore(cfg.max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Lock()
        return self._host_locks[host]

    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
        raise_for_status: bool = True,
        max_bytes: int | None = None,
    ) -> Response:
        host = (urlsplit(url).hostname or "").lower()
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if accept:
            headers["Accept"] = accept

        async with self._global, self._lock(host):
            wait = self._host_last.get(host, 0) + self.cfg.per_host_delay_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._get_capped(url, headers, max_bytes or self.cfg.max_bytes)
            except httpx.TooManyRedirects as e:
                raise HttpError(f"too many redirects: {e}") from e
            except httpx.TimeoutException as e:
                raise NetworkError(f"timeout fetching {url}") from e
            except httpx.HTTPError as e:
                raise NetworkError(f"{type(e).__name__}: {e}") from e
            finally:
                self._host_last[host] = time.monotonic()

        if is_bot_challenge(resp):
            raise HttpError(
                f"{resp.url} answered with a bot-check page (it only admits browsers); Readwell won't impersonate one",
                http_status=resp.status if resp.status >= 400 else 403,
            )
        if raise_for_status and resp.status >= 400:
            raise HttpError(
                f"HTTP {resp.status} from {resp.url}",
                http_status=resp.status,
                retry_after_s=parse_retry_after(resp.headers.get("retry-after"))
                if resp.status in (429, 503)
                else None,
            )
        return resp

    async def _get_capped(self, url: str, headers: dict[str, str], max_bytes: int) -> Response:
        async with self.client.stream("GET", url, headers=headers) as r:
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise TooLargeError(f"response from {url} exceeds {max_bytes} bytes")
                chunks.append(chunk)
            return Response(r.status_code, str(r.url), r.headers, b"".join(chunks))
