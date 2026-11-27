"""Clean article HTML before it goes into a feed entry or a PDF.

Readers take an entry's thumbnail from its first image, so junk images matter: a blank
spacer leaves an empty box, and an in-article ad becomes the "photo" for the story.

- drops images with no usable address, tracking pixels and spacer/blank/transparent gifs
- drops ads: paragraphs or figures labelled "Advertisement"/"Sponsored", and images whose
  class, id or alt says ad / sponsor / banner
- turns lazy-loaded images (``data-src``/``data-lazy-src``) into ordinary ones
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

JUNK_SRC = re.compile(r"(spacer|pixel|blank|transparent|1x1|clear\.gif|tracking|beacon)[^/]*\.(gif|png)"
                      r"|/(pixel|track|open|beacon)(\.gif|\.png|\?)", re.I)
AD_LABEL = re.compile(r"^\s*(advertisement|advertising|sponsored( content| by)?|ad|promoted)\s*[.:]?\s*$", re.I)
AD_CLASS = re.compile(r"\b(ad|ads|advert|advertisement|sponsor(ed|ship)?|banner-?ad|dfp|promo)\b", re.I)
AD_ALT = re.compile(r"\b(advert(isement)?|sponsor(ed|ship)?)\b", re.I)  # alt text: only unambiguous words
LAZY = ("data-src", "data-lazy-src", "data-original", "data-orig-file")


def _tiny(node) -> bool:
    for a in ("width", "height"):
        v = (node.attributes.get(a) or "").strip().rstrip("px")
        if v.isdigit() and int(v) <= 3:
            return True
    return False


def tidy_html(html: str | None) -> str:
    if not html or "<img" not in html and "dvertis" not in html:
        return html or ""
    tree = LexborHTMLParser(f"<div id='rw-root'>{html}</div>")
    root = tree.css_first("#rw-root")
    if root is None:
        return html
    changed = False

    # labelled ad blocks: <p>Advertisement.<br><img …></p>, <figure><figcaption>Sponsored</figcaption>…
    for block in root.css("p, figure, div, aside"):
        if block.css_first("img") is None:
            continue
        own = (block.text(deep=True, separator=" ", strip=True) or "")
        first_line = own.split("  ")[0] if own else ""
        if AD_LABEL.match(first_line) or AD_LABEL.match(own):
            block.decompose()
            changed = True

    for img in root.css("img"):
        src = (img.attributes.get("src") or "").strip()
        if not src.startswith(("http://", "https://")):
            for a in LAZY:
                cand = (img.attributes.get(a) or "").strip()
                if cand.startswith(("http://", "https://")):
                    img.attrs["src"] = src = cand
                    changed = True
                    break
        cls = " ".join(img.attributes.get(a) or "" for a in ("class", "id"))
        if (not src.startswith(("http://", "https://")) or JUNK_SRC.search(src) or _tiny(img)
                or AD_CLASS.search(cls) or AD_ALT.search(img.attributes.get("alt") or "")):
            img.decompose()
            changed = True

    return root.html[len("<div id=\"rw-root\">"):-len("</div>")] if changed and root.html else html
