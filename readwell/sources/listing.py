"""``kind: listing`` — a web page with no feed, described by a recipe of CSS selectors.

    - id: example-assets
      kind: listing
      mode: inventory            # stream (news-like) | inventory (a catalogue, e.g. assets for sale)
      url: https://example.com/assets-for-sale
      item: "div.listing-card"   # one element per item
      fields:                    # selectors are relative to the item; "." is the item itself
        title:   {sel: "h3"}
        link:    {sel: "a", attr: href}
        date:    {sel: "time", attr: datetime, optional: true}
        summary: {sel: "p.excerpt", optional: true}
      report_removals: true      # inventory: publish "Removed from listing: …" when an item disappears
      removal_grace_runs: 2
      full_text: auto            # fetch each item's own page for its text (auto | always | never)
      pages: {template: "{url}?page={page}", max: 10}   # optional: follow numbered pages
      doc_type: gazette          # optional; default article
      document_link: 'a[href$=".pdf"]'   # optional: on each item's page, follow this link and read the PDF

With ``pages``, page 2, 3, … are fetched until a page adds no new items or ``max`` is
reached. Inventory mode needs every page, so removals are only judged on complete runs.

``mode: inventory`` makes the first run a silent baseline, so you only hear about new
listings (``baseline: false`` publishes the current ones too). ``mode: stream`` publishes
what it finds, like a feed.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode

from ..models import FetchResult, HttpError, ParseError, RawItem, SourceError
from ..pipeline.normalize import canonical_url
from ..timeutil import parse_human_date
from .base import SourceContext, register


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sel: str = "."
    attr: str = "text"  # text | html | <attribute name>
    optional: bool = False
    pattern: str | None = None  # optional regex; group 1 (or the whole match) is kept


class PagesCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = "{url}?page={page}"
    start: int = 2
    max: int = 10


class ListingCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str
    mode: Literal["stream", "inventory"] = "stream"
    item: str
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    baseline: bool | None = None  # default: True for inventory, False for stream
    min_items: int = 1
    pages: PagesCfg | None = None
    title_template: str | None = None  # e.g. "Alberta OIC {number} — {title}" (any field names)
    doc_type: str = "article"
    document_link: str | None = None  # read by the enrichment pass


def _get(node: LexborNode, spec: FieldSpec, base_url: str) -> str | None:
    target = node if spec.sel in ("", ".") else node.css_first(spec.sel)
    if target is None:
        return None
    if spec.attr == "text":
        val = re.sub(r"\s+", " ", target.text(separator=" ", strip=True) or "").strip()
    elif spec.attr == "html":
        val = target.html or ""
    else:
        val = (target.attributes.get(spec.attr) or "").strip()
        if spec.attr in ("href", "src") and val:
            val = urljoin(base_url, val)
    if val and spec.pattern:
        m = re.search(spec.pattern, val, re.S)
        val = (m.group(1) if m and m.groups() else m.group(0) if m else "") or ""
    return val or None


def parse_date(s: str | None) -> str | None:
    return parse_human_date(s)


def extract_items(html: str | bytes, page_url: str, c: ListingCfg) -> list[RawItem]:
    tree = HTMLParser(html if isinstance(html, str) else html.decode("utf-8", "replace"))
    items: list[RawItem] = []
    for node in tree.css(c.item):
        vals: dict[str, str | None] = {}
        missing = False
        for name, spec in c.fields.items():
            vals[name] = _get(node, spec, page_url)
            if vals[name] is None and not spec.optional:
                missing = True
        if missing:
            continue
        title = vals.get("title")
        if c.title_template:
            try:
                title = c.title_template.format(**{k: v or "" for k, v in vals.items()}).strip(" —-") or title
            except (KeyError, IndexError, ValueError):
                pass
        link = vals.get("link")
        if not (title or link):
            continue
        guid = canonical_url(link) if link else "h:" + hashlib.sha1((title or "").encode()).hexdigest()
        summary = vals.get("summary")
        items.append(RawItem(
            guid=guid,
            url=link,
            title=title,
            author=vals.get("author"),
            published_at=parse_date(vals.get("date")),
            summary_html=f"<p>{summary}</p>" if summary and "<" not in summary else summary,
            doc_type=c.doc_type,
        ))
    return items


async def _page(ctx: SourceContext, url: str):
    resp = await ctx.fetcher.get(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
    if "html" not in resp.content_type:
        raise ParseError(f"{resp.url} returned {resp.content_type or 'unknown type'}, not a web page")
    return resp


@register("listing")
async def fetch_listing(ctx: SourceContext) -> FetchResult:
    try:
        c = ListingCfg.model_validate(ctx.source.model_dump())
    except ValueError as e:
        raise SourceError(f"listing recipe: {e}") from e
    resp = await _page(ctx, c.url)
    items = extract_items(resp.content, resp.url, c)
    if c.pages and items:
        seen = {i.guid for i in items}
        for n in range(c.pages.start, c.pages.start + c.pages.max - 1):
            try:
                r = await _page(ctx, c.pages.template.format(url=c.url, page=n))
            except HttpError as e:
                if e.http_status in (404, 410):
                    break  # ran past the last page
                raise
            more = extract_items(r.content, r.url, c)
            fresh = [i for i in more if i.guid not in seen]
            if not fresh:
                break
            seen |= {i.guid for i in fresh}
            items += fresh
    if len(items) < c.min_items:
        raise ParseError(f"recipe matched {len(items)} items at {resp.url} — the page layout may have changed")
    inventory = c.mode == "inventory"
    baseline = c.baseline if c.baseline is not None else inventory
    return FetchResult(
        items=items,
        http_status=resp.status,
        baseline=ctx.first_run and baseline,
        present_guids={i.guid for i in items if i.guid} if inventory else None,
    )
