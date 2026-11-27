"""Near-duplicate headline detection by word-set overlap.

Headlines are short, so bit-signature methods (SimHash, MinHash) are noisy at this
length. Comparing the sets of meaningful words is both simpler and more accurate:
two headlines match when almost all of the shorter one's words appear in the other
("…near Ross River" vs "…near Ross River, First Nation says"), but a single swapped
word in a five-word headline ("third quarter" vs "second quarter") does not.
"""

from __future__ import annotations

import re

STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the to was were will with "
    "says said say".split()
)
_WORD = re.compile(r"[a-z0-9]+")


def title_tokens(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    t = text.lower().replace("’", "'").replace("'s", "")
    t = re.sub(r"(?<=\w)\.(?=\w)", "", t).replace(".", " ")  # B.C. -> bc, U.S. -> us
    return frozenset(w for w in _WORD.findall(t) if w not in STOP)


def title_key(text: str | None) -> str | None:
    toks = title_tokens(text)
    return " ".join(sorted(toks)) if toks else None


def similar(a: frozenset[str], b: frozenset[str], *, min_words: int = 5, containment: float = 0.9) -> bool:
    if min(len(a), len(b)) < min_words:
        return False
    common = len(a & b)
    return common / min(len(a), len(b)) >= containment and common / len(a | b) >= 0.5
