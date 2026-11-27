"""Signed action links (👍 / 👎, Send to Supernote) so link-preview bots and crawlers can't act.

The key comes from ``READWELL_SECRET`` or, failing that, a random key kept in the data dir.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

_cache: dict[str, bytes] = {}


def key(data_dir: Path) -> bytes:
    env = os.environ.get("READWELL_SECRET")
    if env:
        return env.encode()
    path = Path(data_dir) / "secret.key"
    k = str(path)
    if k not in _cache:
        try:
            _cache[k] = path.read_bytes().strip()
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh = secrets.token_hex(32).encode()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(fresh)
            _cache[k] = fresh
    return _cache[k]


def sign(data_dir: Path, message: str) -> str:
    return hmac.new(key(data_dir), message.encode(), hashlib.sha256).hexdigest()[:20]


def check(data_dir: Path, message: str, sig: str | None) -> bool:
    return bool(sig) and hmac.compare_digest(sign(data_dir, message), sig or "")
