"""Main-text extraction from article pages (trafilatura)."""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura

from ..pipeline.normalize import clean_html, html_to_text


@dataclass
class Extracted:
    html: str | None
    text: str
    words: int
    title: str | None = None
    author: str | None = None
    date: str | None = None  # YYYY-MM-DD as trafilatura reports it


def extract_article(html: str | bytes, url: str) -> Extracted:
    """Pull the article body out of a full web page. CPU-bound: call via asyncio.to_thread."""
    body = trafilatura.extract(
        html,
        url=url,
        output_format="html",
        include_links=True,
        include_images=True,
        include_tables=True,
        include_comments=False,
        favor_recall=True,
    )
    meta = trafilatura.extract_metadata(html, default_url=url)
    cleaned = clean_html(_strip_wrapper(body)) if body else None
    text = html_to_text(cleaned)
    return Extracted(
        html=cleaned,
        text=text,
        words=len(text.split()),
        title=getattr(meta, "title", None) if meta else None,
        author=getattr(meta, "author", None) if meta else None,
        date=getattr(meta, "date", None) if meta else None,
    )


def _strip_wrapper(html: str) -> str:
    """trafilatura wraps its HTML output in <html><body>…</body></html>."""
    start, end = html.find("<body>"), html.rfind("</body>")
    return html[start + 6:end] if start != -1 and end != -1 else html
