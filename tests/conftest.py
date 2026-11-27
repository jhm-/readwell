from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    """Fixture text with relative dates filled in, so tests never age out of the feed window."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    now = datetime.now(UTC).replace(microsecond=0)
    subs = {
        "{{RFC_2H}}": format_datetime(now - timedelta(hours=2), usegmt=True),
        "{{RFC_8H}}": format_datetime(now - timedelta(hours=8), usegmt=True),
        "{{ISO_1H}}": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "{{ISO_30M}}": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for k, v in subs.items():
        text = text.replace(k, v)
    return text.encode()


class Site:
    """A fake internet for httpx.MockTransport: path → (status, content-type, body)."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str, bytes, dict[str, str]]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, body: bytes | str, ctype: str = "text/html", status: int = 200,
            headers: dict[str, str] | None = None) -> None:
        self.routes[url] = (status, ctype, body if isinstance(body, bytes) else body.encode(), headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url not in self.routes:
            return httpx.Response(404, text="not found")
        status, ctype, body, headers = self.routes[url]
        etag = headers.get("etag")
        if etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(status, content=body, headers={"content-type": ctype, **headers})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def site() -> Site:
    return Site()


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    (config_dir / "sources").mkdir(parents=True)
    data_dir.mkdir()
    (config_dir / "config.yaml").write_text(yaml.safe_dump({
        "fetch": {"per_host_delay_s": 0, "timeout_s": 5},
        "server": {"base_url": "http://readwell.test:8080"},
    }))
    return config_dir, data_dir


def write_sources(config_dir: Path, name: str, doc: object) -> None:
    (config_dir / "sources" / name).write_text(yaml.safe_dump(doc, sort_keys=False))
