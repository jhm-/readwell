"""Source kinds. Each module turns one kind of source into RawItems and registers itself."""

from . import feed, jsonapi, listing, mail, numbered, yesab  # noqa: F401  (each registers its kind)
from .base import REGISTRY, SourceContext

__all__ = ["REGISTRY", "SourceContext"]
