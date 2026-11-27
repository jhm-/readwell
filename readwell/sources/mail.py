"""``kind: email`` — newsletters and alerts from an IMAP mailbox.

The mailbox is opened **read-only**: Readwell never deletes, moves or marks mail. Each
email source keeps its own position (IMAP UID) in ``sources.cursor``; the first run looks
back ``since_days``.

Several email sources can share one mailbox and split it by sender with ``from:``
patterns (``*@substack.com``, ``alerts@scc-csc.ca``). A source whose patterns include
``"*"`` takes whatever no other source on that mailbox claims.

    - id: newsletters
      kind: email
      imap: {host: mail.example.net, user: news@example.net, password_env: READWELL_IMAP_PASSWORD}
      from: ["*"]
      interval_min: 10

The password is read from the environment variable named by ``password_env`` (set in
/etc/readwell/env), never from YAML.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import imaplib
import json
import os
import re
from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html import escape
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import FetchResult, NetworkError, RawItem, SourceError
from ..timeutil import now, to_iso
from .base import SourceContext, dump_cursor, register

MAX_MESSAGES_PER_RUN = 200


class ImapCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = 993
    user: str
    password_env: str = "READWELL_IMAP_PASSWORD"
    folder: str = "INBOX"
    ssl: bool = True


class EmailSourceCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    imap: ImapCfg
    from_: list[str] = Field(default_factory=lambda: ["*"], alias="from")
    since_days: int = 14


def _mailbox_key(c: EmailSourceCfg) -> tuple[str, str, str]:
    return (c.imap.host.lower(), c.imap.user.lower(), c.imap.folder)


def sender_matches(addr: str, patterns: list[str]) -> bool:
    addr = addr.lower()
    return any(fnmatch.fnmatchcase(addr, p.lower()) for p in patterns if p != "*")


# --------------------------------------------------------------------------- IMAP (sync, run in a thread)


def fetch_mailbox(imap: ImapCfg, password: str, cursor: dict, since_days: int) -> tuple[str, list[tuple[int, bytes]]]:
    """Return (uidvalidity, [(uid, raw message)]) for messages after the cursor. Read-only."""
    cls = imaplib.IMAP4_SSL if imap.ssl else imaplib.IMAP4
    m = cls(imap.host, imap.port, timeout=60)
    try:
        m.login(imap.user, password)
        typ, _ = m.select(f'"{imap.folder}"', readonly=True)
        if typ != "OK":
            raise SourceError(f"cannot open folder {imap.folder!r}")
        uidvalidity = (m.response("UIDVALIDITY")[1] or [b"0"])[0]
        uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)
        last = int(cursor.get("last_uid", 0)) if cursor.get("uidvalidity") == uidvalidity else 0
        if last:
            typ, data = m.uid("SEARCH", None, f"UID {last + 1}:*")
        else:
            since = (now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            typ, data = m.uid("SEARCH", None, f"SINCE {since}")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last)[:MAX_MESSAGES_PER_RUN]
        out = []
        for uid in uids:
            typ, msg = m.uid("FETCH", str(uid), "(BODY.PEEK[])")  # PEEK: don't set \\Seen
            raw = next((part[1] for part in msg if isinstance(part, tuple)), None)
            if raw:
                out.append((uid, raw))
        return uidvalidity, out
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001 - best effort
            pass


# --------------------------------------------------------------------------- parsing

WEB_LINK_TEXT = re.compile(
    r"(view|read|open)\s+(this\s+)?(email|post|newsletter|issue|it|online)?\s*(in|on)?\s*(a|your)?\s*"
    r"(web\s*)?(browser|online|web(site)?|app)\b",
    re.I,
)
POST_URL = re.compile(r"^https?://[^/]+/p/[a-z0-9][a-z0-9-]*", re.I)  # Substack, Beehiiv, Ghost-style
BOILERPLATE = re.compile(
    r"(unsubscribe|manage (your )?(subscription|preferences|email settings)|update (your )?preferences|"
    r"view (this email |it )?in (your |a )?browser|you('re| are) receiving this|you received this|"
    r"forward(ed)? (this|to a friend)|subscribe now|upgrade to paid|leave a comment|"
    r"^\s*(share|like|restack|comment|read in app)\s*$|why did i get this|sent to you because)",
    re.I,
)
CONFIRM = re.compile(r"\b(confirm|verify|activate)\b.{0,40}\b(subscription|e-?mail|address|sign[- ]?up)\b|"
                     r"please confirm", re.I)


def unwrap_redirect(href: str) -> str:
    """Recover the destination of common tracking redirects without visiting them."""
    try:
        p = urlsplit(href)
    except ValueError:
        return href
    for key in ("url", "u", "redirect", "redirect_url", "target", "dest", "destination"):
        for v in parse_qs(p.query).get(key, []):
            if v.startswith(("http://", "https://")):
                return v
    # Substack: substack.com/redirect/2/<base64 json with "e": destination>
    m = re.search(r"/redirect/\d+/([A-Za-z0-9_-]+={0,2})", p.path)
    if m:
        try:
            blob = json.loads(base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
            dest = blob.get("e") or blob.get("url")
            if isinstance(dest, str) and dest.startswith(("http://", "https://")):
                return dest
        except (ValueError, TypeError):
            pass
    return href


def web_url(html: str) -> str | None:
    """The newsletter's own web address (e.g. the Substack post), if the email links to it."""
    tree = HTMLParser(html)
    links = [(unwrap_redirect(a.attributes.get("href") or ""), a.text(strip=True)) for a in tree.css("a[href]")]
    for href, _ in links:
        m = POST_URL.match(href)
        if m:
            return m.group(0)
    for href, text in links:
        if href.startswith(("http://", "https://")) and WEB_LINK_TEXT.search(text or ""):
            return href.split("#")[0]
    return None


def clean_newsletter(html: str) -> str:
    tree = HTMLParser(html)
    for img in tree.css("img"):
        w, h = img.attributes.get("width") or "", img.attributes.get("height") or ""
        if w.strip() in ("0", "1", "2") or h.strip() in ("0", "1", "2"):
            img.decompose()  # tracking pixels
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        new = unwrap_redirect(href)
        if new != href:
            a.attrs["href"] = new
    for tag in ("a", "p", "span", "li", "td", "div", "table"):
        for node in tree.css(tag):
            try:
                text = node.text(separator=" ", strip=True)
            except Exception:  # noqa: BLE001 - node already removed with an ancestor
                continue
            if text and len(text.split()) <= 40 and BOILERPLATE.search(text):
                node.decompose()
    body = tree.body
    return (body.inner_html if body is not None else tree.html) or ""


def parse_message(raw: bytes) -> RawItem:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
    subject = re.sub(r"\s+", " ", str(msg.get("subject", "") or "")).strip() or "(no subject)"
    name, addr = parseaddr(str(msg.get("from", "") or ""))
    try:
        published = to_iso(parsedate_to_datetime(str(msg.get("date"))))
    except (TypeError, ValueError, IndexError):
        published = None
    part = msg.get_body(preferencelist=("html", "plain"))
    content = ""
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            content = part.get_payload(decode=True).decode("utf-8", "replace") if part.get_payload() else ""
        if part.get_content_type() == "text/plain":
            content = "".join(f"<p>{escape(p.strip())}</p>" for p in re.split(r"\n\s*\n", content) if p.strip())
    url = web_url(content) if content else None
    mid = str(msg.get("message-id", "") or "").strip()
    guid = "mid:" + mid if mid else "h:" + hashlib.sha1(raw[:4096]).hexdigest()
    if CONFIRM.search(subject):
        subject = "[Action needed] " + subject
    return RawItem(
        guid=guid,
        url=url,
        title=subject,
        author=(f"{name} <{addr}>" if name else addr) or None,
        published_at=published,
        content_html=clean_newsletter(content) if content else None,
        doc_type="newsletter",
    )


# --------------------------------------------------------------------------- source


@register("email")
async def fetch_email(ctx: SourceContext) -> FetchResult:
    try:
        ec = EmailSourceCfg.model_validate(ctx.source.model_dump())
    except Exception as e:  # noqa: BLE001 - surface config problems as source errors
        raise SourceError(f"email source config: {e}") from e
    password = os.environ.get(ec.imap.password_env)
    if not password:
        raise SourceError(f"environment variable {ec.imap.password_env} is not set (add it to /etc/readwell/env)")

    # patterns claimed by other email sources on the same mailbox (for this source's "*")
    claimed: list[str] = []
    for other in ctx.cfg.sources.values():
        if other.id == ctx.source.id or other.kind != "email" or not other.enabled:
            continue
        try:
            oc = EmailSourceCfg.model_validate(other.model_dump())
        except Exception:  # noqa: BLE001
            continue
        if _mailbox_key(oc) == _mailbox_key(ec):
            claimed += [p for p in oc.from_ if p != "*"]

    try:
        uidvalidity, messages = await asyncio.to_thread(fetch_mailbox, ec.imap, password, ctx.cursor, ec.since_days)
    except imaplib.IMAP4.error as e:
        raise SourceError(f"IMAP: {e}") from e
    except OSError as e:
        raise NetworkError(f"IMAP {ec.imap.host}: {e}") from e

    items: list[RawItem] = []
    last_uid = int(ctx.cursor.get("last_uid", 0)) if ctx.cursor.get("uidvalidity") == uidvalidity else 0
    for uid, raw in messages:
        last_uid = max(last_uid, uid)
        _, addr = parseaddr(BytesParser(policy=policy.default).parsebytes(raw, headersonly=True).get("from", ""))
        mine = sender_matches(addr, ec.from_) or ("*" in ec.from_ and not sender_matches(addr, claimed))
        if mine:
            items.append(parse_message(raw))
    return FetchResult(
        items=items,
        cursor=dump_cursor({"uidvalidity": uidvalidity, "last_uid": last_uid}),
        expect_items=False,
        feed_title=ec.imap.user,
    )
