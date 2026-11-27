"""``kind: numbered`` — documents published at sequentially numbered URLs (orders-in-council).

    - id: yukon-oic
      kind: numbered
      url_template: "https://laws.example/{year}/{year}-{n:04d}/{year}-{n:04d}_1.pdf"
      start: {year: 2026, n: 1}
      title: "Yukon OIC {year}/{n:02d}"   # the act name found in the text is appended
      expect: pdf                         # pdf | html
      probe_ahead: 5                      # consecutive misses before stopping
      year_rollover: true                 # in a new calendar year, also try {year+1} from n=1
      doc_type: oic

Each run starts after the last number found (kept in the source's cursor) and keeps
going until ``probe_ahead`` numbers in a row are missing. A number counts as missing on
HTTP 404/410, on content of the wrong type (an HTML error page instead of a PDF), when
``must_contain`` doesn't match, or when ``miss_text`` does.

The first run is a baseline: it catches up to the latest number without publishing the
back catalogue (``baseline: false`` publishes it instead).
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..extract.document import find_act, html_doc_text, looks_like_pdf, pdf_text
from ..models import FetchResult, HttpError, RawItem, SourceError
from ..timeutil import now, parse_human_date
from .base import SourceContext, dump_cursor, register

_cpu = asyncio.Semaphore(1)


class Start(BaseModel):
    year: int
    n: int = 1


class NumberedCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url_template: str
    start: Start
    title: str = "{year}-{n}"
    append_act: bool = True  # add the first act named in the text to the title (off for whole gazette issues)
    expect: Literal["pdf", "html"] = "pdf"
    probe_ahead: int = 5
    year_rollover: bool = True
    must_contain: str | None = None
    miss_text: str | None = None
    doc_type: str = "document"
    baseline: bool = True
    max_per_run: int = 50
    baseline_max: int = 1500
    date_pattern: str | None = None  # regex whose group 1 is the document's date, e.g. "Approved and Ordered (\\w+ \\d+, \\d{4})"


def _url(c: NumberedCfg, year: int, n: int) -> str:
    return c.url_template.format(year=year, n=n)


async def _probe(ctx: SourceContext, c: NumberedCfg, year: int, n: int) -> RawItem | None:
    url = _url(c, year, n)
    try:
        r = await ctx.fetcher.get(url, accept="application/pdf,text/html;q=0.9,*/*;q=0.5")
    except HttpError as e:
        if e.http_status in (404, 410):
            return None
        raise
    body = r.content
    if c.expect == "pdf":
        if not looks_like_pdf(body):
            return None
        async with _cpu:
            doc = await asyncio.to_thread(pdf_text, body)
    else:
        if looks_like_pdf(body) or "html" not in r.content_type:
            return None
        page = body.decode("utf-8", "replace")
        if c.must_contain and not re.search(c.must_contain, page, re.I):
            return None
        if c.miss_text and re.search(c.miss_text, page, re.I):
            return None
        async with _cpu:
            doc = await asyncio.to_thread(html_doc_text, body, r.url)
    title = c.title.format(year=year, n=n)
    act = find_act(doc.text) if doc.text and c.append_act else None
    if act and act.lower() not in title.lower():
        title = f"{title} — {act}"
    published = None
    if c.date_pattern and doc.text:
        m = re.search(c.date_pattern, doc.text)
        published = parse_human_date(m.group(1)) if m else None
    return RawItem(guid=f"{year}-{n:04d}", url=url, title=title, content_html=doc.html, doc_type=c.doc_type,
                   published_at=published)


async def _scan(ctx: SourceContext, c: NumberedCfg, year: int, after: int, budget: int) -> tuple[list[RawItem], int]:
    items: list[RawItem] = []
    last, misses, n = after, 0, after + 1
    while misses < c.probe_ahead and len(items) < budget:
        item = await _probe(ctx, c, year, n)
        if item is None:
            misses += 1
        else:
            items.append(item)
            last, misses = n, 0
        n += 1
    return items, last


@register("numbered")
async def fetch_numbered(ctx: SourceContext) -> FetchResult:
    try:
        c = NumberedCfg.model_validate(ctx.source.model_dump())
        _url(c, c.start.year, c.start.n)
    except (ValueError, KeyError, IndexError) as e:
        raise SourceError(f"numbered source config: {e}") from e

    cur = ctx.cursor
    year = int(cur.get("year", c.start.year))
    last = int(cur.get("n", c.start.n - 1))
    first = ctx.first_run
    budget = c.baseline_max if first else c.max_per_run

    items, last = await _scan(ctx, c, year, last, budget)
    if c.year_rollover and now().year > year and len(items) < budget:
        nxt, nxt_last = await _scan(ctx, c, year + 1, 0, budget - len(items))
        if nxt:
            items += nxt
            year, last = year + 1, nxt_last

    return FetchResult(
        items=items,
        cursor=dump_cursor({"year": year, "n": last}),
        baseline=first and c.baseline,
        expect_items=False,
    )
