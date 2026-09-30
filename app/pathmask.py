"""Mask ID-like path segments before anything is stored.

The tracker sends `location.pathname`, and on many sites the path itself is a
key: an order page (`/order/<id>`), a password-reset or support link
(`/reset/<uid>/<token>`), a device or card number. Storing such a path keeps a
working link, or at least an identifier, in raw events for the whole retention
window. So every ID-like segment is replaced by `:id` on the server, where the
promise can be enforced, whatever the sender does.

The rules are generic (no per-site patterns). A segment is ID-like if it holds:

  * a UUID,
  * a run of 12 or more digits (card, device and long numeric IDs),
  * a run of 12 or more hex characters with at least one digit in it,
  * an alphanumeric run of 16 or more characters that mixes letters and digits,
  * a URL-safe token of 16 or more characters (base64url and similar) that
    mixes letters and digits and changes character class often, which is what
    random data does and a readable slug does not,
  * an e-mail address.

Readable slugs (`/guide/esim-tyrkia`, `/pakker/TR`), short numbers and dates
are kept. The whole segment becomes `:id`, not just the matching run, so a
token wrapped in other text cannot leave half of itself behind. Masking is
idempotent: masking a masked path changes nothing.
"""

from __future__ import annotations

import re

MASK = ":id"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_LONG_DIGITS = re.compile(r"[0-9]{12,}")
_HEX_RUN = re.compile(r"[0-9A-Fa-f]{12,}")
_URLSAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,}")
_EMAIL = re.compile(r"[^@/\s]+(?:@|%40)[^@/\s]+\.[A-Za-z]{2,}", re.I)
_SEPARATORS = re.compile(r"[-_]")
_WORD = re.compile(r"[^/\s]+")

_MIN_HEX = 12
_MIN_MIXED_RUN = 16


def _has_digit(s: str) -> bool:
    return any(c.isdigit() for c in s)


def _has_letter(s: str) -> bool:
    return any(c.isalpha() for c in s)


def _char_class(c: str) -> int:
    if c.isdigit():
        return 0
    return 1 if c.isupper() else 2


def _class_changes(token: str) -> int:
    """How often the character class (digit, upper, lower) changes inside the
    token's parts. Random base64url data changes class on most characters; a
    slug like `iphone-15-pro-256gb` barely does."""
    n = 0
    for part in _SEPARATORS.split(token):
        n += sum(1 for a, b in zip(part, part[1:]) if _char_class(a) != _char_class(b))
    return n


def _id_like_run(run: str) -> bool:
    """One alphanumeric run (no separators inside)."""
    if _LONG_DIGITS.search(run):
        return True
    if any(_has_digit(m.group()) for m in _HEX_RUN.finditer(run)):
        return True
    return len(run) >= _MIN_MIXED_RUN and _has_digit(run) and _has_letter(run)


def _random_token(segment: str) -> bool:
    """A URL-safe token whose separators split it into short runs, for example
    base64url with `-` or `_` inside. Readable slugs fail the class-change test."""
    if not _URLSAFE_TOKEN.fullmatch(segment):
        return False
    if not (_has_digit(segment) and _has_letter(segment)):
        return False
    return _class_changes(segment) >= max(5, -(-len(segment) // 3))


def is_id_like(segment: str) -> bool:
    """True if this one path segment (no `/`) carries an identifier."""
    if not segment or segment == MASK:
        return False
    if _UUID.search(segment) or _EMAIL.search(segment):
        return True
    if any(_id_like_run(r) for r in _ALNUM_RUN.findall(segment)):
        return True
    return _random_token(segment)


def mask_path(path: str) -> str:
    """Replace every ID-like segment of a URL path with `:id`.

    >>> mask_path("/order/3f2a9b1c7d4e5f60/receipt.pdf")
    '/order/:id/receipt.pdf'
    >>> mask_path("/guide/esim-tyrkia")
    '/guide/esim-tyrkia'
    """
    if not isinstance(path, str) or not path:
        return path
    return "/".join(MASK if is_id_like(s) else s for s in path.split("/"))


def mask_label(text: str | None) -> str | None:
    """Same rules for a free-text label (a custom event name): every word, split
    on whitespace and `/`, that is ID-like becomes `:id`."""
    if not isinstance(text, str) or not text:
        return text
    return _WORD.sub(lambda m: MASK if is_id_like(m.group()) else m.group(), text)
