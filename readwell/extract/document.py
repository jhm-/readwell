"""Text from official documents: PDFs (text layer) and HTML pages."""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from html import escape

from pypdf import PdfReader
from pypdf.errors import PdfReadError, PyPdfError

from ..pipeline.normalize import clean_html, html_to_text
from .article import extract_article

ACT = re.compile(r"\b((?:[A-Z][A-Za-z'’\-]+,? (?:and |of |the |de |du )?){1,7}Act)\b")
log = logging.getLogger(__name__)
# pypdf logs a warning per quirk in a malformed font table — thousands per document on some
# government PDFs. Its real failures arrive as exceptions, which pdf_text handles.
logging.getLogger("pypdf").setLevel(logging.ERROR)

SCANNED = "<p><i>Scanned document with no text layer — open the PDF to read it.</i></p>"


@dataclass
class DocText:
    text: str
    html: str
    pages: int = 0
    scanned: bool = False


def looks_like_pdf(content: bytes) -> bool:
    return content[:1024].lstrip().startswith(b"%PDF")


def pdf_text(content: bytes, max_pages: int = 60) -> DocText:
    """CPU-bound: call via asyncio.to_thread."""
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = reader.pages[:max_pages]
    except (PyPdfError, PdfReadError, ValueError, KeyError, TypeError) as e:
        return DocText(text="", html=f"<p><i>Could not read this PDF ({escape(str(e)[:120])}).</i></p>")
    parts, bad = [], 0
    for p in pages:
        try:
            parts.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001 — one malformed page (e.g. pypdf's token limit) shouldn't lose the rest
            bad += 1
    if bad:
        log.info("PDF: skipped %d unreadable page(s) of %d", bad, len(pages))
    text = _tidy("\n\n".join(parts))
    if len(text) < 40:
        return DocText(text="", html=SCANNED, pages=len(reader.pages), scanned=True)
    return DocText(text=text, html=text_to_html(text), pages=len(reader.pages))


def html_doc_text(html: str | bytes, url: str) -> DocText:
    ext = extract_article(html, url)
    if ext.words >= 20 and ext.html:
        return DocText(text=ext.text, html=ext.html)
    raw = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    cleaned = clean_html(raw) or ""
    text = html_to_text(cleaned)
    return DocText(text=text, html=text_to_html(text))


def text_to_html(text: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join("<p>" + escape(p).replace("\n", "<br>") + "</p>" for p in paras)


def find_act(text: str) -> str | None:
    m = ACT.search(text[:4000])
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def _tidy(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
