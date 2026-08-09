"""Title -> slug conversion.

Slugs keep non-ASCII letters as they are, so Korean titles stay readable
instead of collapsing into a row of hyphens.
"""

from __future__ import annotations

import re
import unicodedata

from .errors import InvalidSlug

MAX_SLUG_LENGTH = 200

_SEPARATORS = re.compile(r"[\s/\\]+")
_REPEATED_DASH = re.compile(r"-{2,}")
_ALLOWED_EXTRA = frozenset("-_.")


def slugify(value: str, *, strict: bool = True) -> str:
    """Normalise `value` into a slug.

    With ``strict=False`` an unusable title yields an empty string instead of
    raising, which is what the wikilink parser wants.
    """
    text = unicodedata.normalize("NFC", value).strip().lower()
    text = _SEPARATORS.sub("-", text)
    slug = "".join(ch if ch.isalnum() or ch in _ALLOWED_EXTRA else "-" for ch in text)
    slug = _REPEATED_DASH.sub("-", slug).strip("-.")[:MAX_SLUG_LENGTH].strip("-.")
    if not slug and strict:
        raise InvalidSlug(value)
    return slug
