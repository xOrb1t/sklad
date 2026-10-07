"""Parse quantity input: ``12`` sets the value, ``+5`` / ``-3`` adjust it."""
from __future__ import annotations

import re

_QTY_RE = re.compile(r"^([+-]?)\s*(\d{1,9})$")


def parse_quantity(text: str | None, current: int | None = None) -> int | None:
    """Return the new quantity, or None when *text* is invalid or the result is negative.

    Relative input (``+N`` / ``-N``) needs *current*; without it only absolute
    values are accepted.
    """
    match = _QTY_RE.match((text or "").strip())
    if not match:
        return None
    sign, digits = match.groups()
    value = int(digits)
    if sign:
        if current is None:
            return None
        value = current + value if sign == "+" else current - value
    return value if value >= 0 else None
