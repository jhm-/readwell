"""Turn RawItems into clean, comparable records."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import RawItem
from .titles import title_key

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid", "igshid",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "oly_anon_id", "oly_enc_id", "vero_id",
    "ref", "ref_src", "ref_url", "referrer", "source", "cmpid", "cmp", "s_cid", "ncid", "ocid",
    "spm", "smid", "sh", "share", "taid", "rss", "via", "amp",
}
TRACKING_PREFIXES = ("utm_", "at_", "pk_", "piwik_", "mtm_", "hsa_", "__s")
DROP_TAGS = ("script", "style", "iframe", "object", "embed", "form", "noscript", "template")


def canonical_url(url: str | None) -> str | None:
    """Comparison key for a URL: tracking parameters, fragments and cosmetic differences removed.

    This is a dedupe key, not a display URL — the original URL is kept for links.
    """
    if not url:
        return None
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if p.scheme not in ("http", "https") or not p.hostname:
        return url.strip()
    host = p.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("amp."):
        host = host[4:]
    port = p.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", p.path or "/")
    path = re.sub(r"/amp/?$", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    query.sort()
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def clean_html(html: str | None) -> str | None:
    """Remove active content (scripts, iframes, forms, event handlers) from feed HTML."""
    if not html or not html.strip():
        return None
    tree = HTMLParser(html)
    for tag in DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css("*"):
        attrs = node.attributes
        for name in list(attrs):
            low = name.lower()
            if low.startswith("on") or (low in ("href", "src") and (attrs[name] or "").strip().lower().startswith("javascript:")):
                del node.attrs[name]
    body = tree.body
    out = body.inner_html if body is not None else tree.html
    return (out or "").strip() or None


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    tree = HTMLParser(html)
    for tag in DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    text = tree.text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Normalized:
    guid: str
    url: str | None
    canonical_url: str | None
    title: str | None
    author: str | None
    published_at: str | None
    summary: str | None
    content_html: str | None
    content_text: str
    word_count: int
    content_hash: str | None
    title_key: str | None
    doc_type: str


def normalize(raw: RawItem) -> Normalized:
    content = clean_html(raw.content_html)
    summary = clean_html(raw.summary_html)
    # if the "summary" is the whole article and there is no separate content, treat it as content
    body = content or summary
    text = html_to_text(body)
    title = re.sub(r"\s+", " ", raw.title).strip() if raw.title else None
    if title:
        title = html_to_text(title) or title  # some feeds put markup/entities in titles
    canon = canonical_url(raw.url)
    guid = (raw.guid or canon or raw.url or "").strip()
    if not guid:
        guid = "h:" + hashlib.sha1(f"{title}|{raw.published_at}|{text[:200]}".encode()).hexdigest()
    return Normalized(
        guid=guid[:2000],
        url=raw.url,
        canonical_url=canon,
        title=title,
        author=raw.author,
        published_at=raw.published_at,
        summary=summary if content else None,
        content_html=body,
        content_text=text,
        word_count=len(text.split()),
        content_hash=hashlib.sha1(text.lower().encode()).hexdigest() if text else None,
        title_key=title_key(title),
        doc_type=raw.doc_type,
    )
