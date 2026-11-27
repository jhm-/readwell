"""``kind: yesab`` — activity on projects in the YESAB Online Registry (yesabregistry.ca).

The registry is a JavaScript app over a public JSON API. Its project list has no "last
updated" date, so each run reads the list, then the activity feeds of a batch of projects:
projects never read before first (new submissions show up quickly), then the rest in turn.

    - id: yesab
      kind: yesab
      url: https://yesabregistry.ca
      activity_per_run: 30      # project activity feeds read per run (all ~180 every few hours)
      first_visit_days: 14      # on a project's first read, only publish activity this recent
      interval_min: 60

Each activity event ("Project Submitted", "Adequacy Review started", "4 documents
published", comment periods, recommendations) becomes one item. The first run reads every project once, silently (a few minutes).
"""

from __future__ import annotations

import hashlib
import json
import re
from html import escape
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..models import FetchResult, HttpError, ParseError, RawItem, SourceError
from ..timeutil import iso_ago
from .base import SourceContext, dump_cursor, register
from .jsonapi import to_date


class YesabCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str = "https://yesabregistry.ca"
    activity_per_run: int = 30
    first_visit_days: int = 14


async def _json(ctx: SourceContext, url: str) -> Any:
    resp = await ctx.fetcher.get(url, accept="application/json")
    try:
        return json.loads(resp.content)
    except ValueError as e:
        raise ParseError(f"{resp.url} didn't return JSON ({resp.content_type or 'unknown type'})") from e


def _message(parts: Any, base: str) -> tuple[str, str]:
    """(plain text, html) from the registry's message parts."""
    texts, htmls = [], []
    for p in parts if isinstance(parts, list) else []:
        if not isinstance(p, dict):
            continue
        msg = str(p.get("message") or "").strip()
        if not msg:
            continue
        if p.get("messagePartType") == "Link" and p.get("linkTo"):
            href = p["linkTo"] if str(p["linkTo"]).startswith("http") else base + str(p["linkTo"])
            htmls.append(f'<a href="{escape(href)}">{escape(msg)}</a>')
        else:
            texts.append(msg)
            htmls.append(escape(msg))
    return " ".join(texts).strip(), " ".join(htmls).strip()


def activity_items(project: dict, events: Any, base: str, since: str | None) -> list[RawItem]:
    out = []
    pid = project.get("projectId")
    number, title = project.get("projectNumber") or "", (project.get("title") or "").strip()
    page = f"{base}/projects/{pid}/activity"
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        when = to_date(ev.get("activityDate"))
        if since and (not when or when < since):
            continue
        text, html = _message(ev.get("message"), base)
        if not text and not html:
            continue
        body = [f"<p><b>{escape(text or 'Activity')}</b></p>" if not html else f"<p><b>{html}</b></p>"]
        meta = [x for x in (f"YESAB project {number}", title, project.get("proponentName"), ev.get("stageGrouping")) if x]
        body.append(f"<p>{escape(' · '.join(meta))}</p>")
        if ev.get("moreMessage"):
            body.append(str(ev["moreMessage"]))
        docs = [d for d in ev.get("documents") or [] if isinstance(d, dict)]
        names = [d.get("name") or d.get("fileName") or d.get("title") for d in docs]
        if any(names):
            body.append("<ul>" + "".join(f"<li>{escape(str(n))}</li>" for n in names if n) + "</ul>")
        key = hashlib.sha1(f"{pid}|{ev.get('activityDate')}|{text}".encode()).hexdigest()[:16]
        out.append(RawItem(
            guid=f"yesab:{pid}:{key}",
            url=page,
            title=f"YESAB {number} {title}: {re.sub(r'\s+', ' ', text or 'activity')}".strip(),
            published_at=when,
            content_html="".join(body),
            doc_type="assessment",
        ))
    return out


@register("yesab")
async def fetch_yesab(ctx: SourceContext) -> FetchResult:
    try:
        c = YesabCfg.model_validate(ctx.source.model_dump())
    except ValueError as e:
        raise SourceError(f"yesab source: {e}") from e
    base = c.url.rstrip("/")
    listing = await _json(ctx, f"{base}/api/projects/search")
    projects = [p for p in (listing.get("projects") if isinstance(listing, dict) else None) or []
                if isinstance(p, dict) and p.get("projectId")]
    if not projects:
        raise ParseError(f"{base}/api/projects/search returned no projects — the registry may have changed")
    projects.sort(key=lambda p: p.get("projectNumber") or "", reverse=True)

    cur = ctx.cursor
    visited: set[str] = set(cur.get("visited") or [])
    offset = int(cur.get("offset") or 0)
    fresh = [p for p in projects if p["projectId"] not in visited]
    rest = [p for p in projects if p["projectId"] in visited]
    if rest:
        offset %= len(rest)
        rest = rest[offset:] + rest[:offset]
    # the first run reads every project once, silently, so later runs only bring news
    batch = fresh + rest if ctx.first_run else (fresh + rest)[: c.activity_per_run]
    rest_ids = {p["projectId"] for p in rest}

    items: list[RawItem] = []
    recent = iso_ago(days=c.first_visit_days)
    for p in batch:
        try:
            events = await _json(ctx, f"{base}/api/projects/{p['projectId']}/activity-feed")
        except HttpError as e:
            if e.http_status in (403, 404, 410):  # withdrawn or hidden project: skip it this time
                continue
            raise
        first_visit = p["projectId"] not in visited
        items += activity_items(p, events, base, recent if first_visit and not ctx.first_run else None)
        visited.add(p["projectId"])
    read_rest = sum(1 for p in batch if p["projectId"] in rest_ids)
    live = {p["projectId"] for p in projects}
    return FetchResult(
        items=items,
        cursor=dump_cursor({"offset": offset + read_rest, "visited": sorted(visited & live)}),
        baseline=ctx.first_run,
        expect_items=False,
    )
