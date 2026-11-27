"""``kind: json`` — a public JSON API, mapped to items with templates.

    - id: eao-activity
      kind: json
      url: https://projects.eao.gov.bc.ca/demi-search/search?dataset=RecentActivity&top=true
      items: "0.searchResults"           # dotted path to the list of records ("" = the top level)
      guid:  "{_id}"
      title: "{project.name}: {headline}"
      link:  "https://projects.eao.gov.bc.ca/p/{project._id}/project-details"
      date:  "{publishDate||dateAdded}"   # ISO text, a human date, or epoch seconds/milliseconds
      body:  "{content}"                  # HTML or text
      doc_type: notice
      baseline: true                     # first run records what's there without publishing it

``link`` may also be a list of templates; the first that gives a value is used.

Templates: ``{a.b.0.c}`` reads a dotted path; ``{a||b}`` takes the first non-empty value;
``{x|date}`` formats a date ("Nov 17, 2026"); ``{x|html}`` escapes text for HTML. A template
that is only one ``{…}`` keeps the value as it is (so a date stays a number).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from html import escape
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ..models import FetchResult, ParseError, RawItem, SourceError
from ..timeutil import parse_human_date, to_iso
from .base import SourceContext, register

_FIELD = re.compile(r"\{([^{}]+)\}")


def lookup(obj: Any, path: str) -> Any:
    cur = obj
    for part in [p for p in path.strip().split(".") if p != ""]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            cur = cur[i] if -len(cur) <= i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def to_date(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        if n > 1e11:  # milliseconds
            n /= 1000
        return to_iso(datetime.fromtimestamp(n, UTC))
    return parse_human_date(str(v))


def _value(record: Any, expr: str) -> Any:
    paths, *filters = re.split(r"(?<!\|)\|(?!\|)", expr)  # "a||b|date" -> "a||b", ["date"]
    val = None
    for path in paths.split("||"):
        val = lookup(record, path)
        if val not in (None, "", "null"):
            break
        val = None
    for f in filters:
        f = f.strip()
        if f == "date":
            iso = to_date(val)
            val = datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d, %Y") if iso else ""
        elif f == "html":
            val = escape(str(val)) if val is not None else ""
    return val


def render(template: str | None, record: Any) -> Any:
    if not template:
        return None
    whole = _FIELD.fullmatch(template.strip())
    if whole:
        return _value(record, whole.group(1))
    return _FIELD.sub(lambda m: "" if (v := _value(record, m.group(1))) is None else str(v), template).strip()


class JsonCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str
    items: str = ""
    guid: str | None = None
    title: str
    link: str | list[str] | None = None  # a list: the first template that gives a value wins
    date: str | None = None
    body: str | None = None
    author: str | None = None
    doc_type: str = "article"
    mode: Literal["stream", "inventory"] = "stream"
    baseline: bool = True


def records(doc: Any, path: str) -> list[Any]:
    found = lookup(doc, path) if path else doc
    if not isinstance(found, list):
        raise ParseError(f"'items: {path}' doesn't point at a list in the response")
    return found


def to_item(rec: Any, c: JsonCfg) -> RawItem | None:
    title = render(c.title, rec)
    if not title:
        return None
    links = c.link if isinstance(c.link, list) else [c.link]
    link = next((v for v in (render(t, rec) for t in links if t) if v), None)
    guid = render(c.guid, rec) if c.guid else None
    if not guid:
        guid = link or "h:" + hashlib.sha1(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()
    body = render(c.body, rec) if c.body else None
    if body is not None and not isinstance(body, str):
        body = str(body)
    if body and "<" not in body:
        body = "".join(f"<p>{escape(p.strip())}</p>" for p in re.split(r"\n\s*\n", body) if p.strip())
    return RawItem(
        guid=str(guid),
        url=str(link) if link else None,
        title=re.sub(r"\s+", " ", str(title)).strip(),
        author=(str(render(c.author, rec)) or None) if c.author else None,
        published_at=to_date(render(c.date, rec)) if c.date else None,
        content_html=body or None,
        doc_type=c.doc_type,
    )


@register("json")
async def fetch_json(ctx: SourceContext) -> FetchResult:
    try:
        c = JsonCfg.model_validate(ctx.source.model_dump())
    except ValueError as e:
        raise SourceError(f"json source: {e}") from e
    resp = await ctx.fetcher.get(c.url, accept="application/json")
    try:
        doc = json.loads(resp.content)
    except ValueError as e:
        raise ParseError(f"{resp.url} didn't return JSON ({resp.content_type or 'unknown type'})") from e
    items = [i for i in (to_item(r, c) for r in records(doc, c.items)) if i is not None]
    return FetchResult(
        items=items,
        http_status=resp.status,
        baseline=ctx.first_run and c.baseline,
        present_guids={i.guid for i in items} if c.mode == "inventory" else None,
        expect_items=False,
    )
