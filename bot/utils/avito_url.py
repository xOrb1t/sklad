"""Utility helpers for Avito URLs."""
from __future__ import annotations

import re

# Avito item URLs end with a 6+ digit item ID, optionally followed by query/fragment.
# Real listings use underscores (iphone_15_pro_3456789012); slash- and
# hyphen-separated IDs are accepted too, plus optional ?query or #anchor.
_ITEM_ID_RE = re.compile(r'[/_-](\d{6,})(?:[?#].*)?$')


def extract_avito_item_id(url: str | None) -> str | None:
    """Return the Avito item ID from *url*, or ``None`` if the URL is not a valid item link.

    Examples::

        >>> extract_avito_item_id("https://www.avito.ru/moskva/telefony/iphone-3456789012")
        '3456789012'
        >>> extract_avito_item_id("https://www.avito.ru/moskva/telefony/iphone_15_pro_3456789012?context=x")
        '3456789012'
        >>> extract_avito_item_id("not-a-url")
        None
        >>> extract_avito_item_id(None)
        None
    """
    if not url:
        return None
    match = _ITEM_ID_RE.search(url)
    return match.group(1) if match else None
