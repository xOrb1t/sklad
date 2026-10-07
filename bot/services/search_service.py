"""Search service — query products by SKU, Avito URL, free text or keywords.

All functions are read-only (SELECT) and operate directly on an
``AsyncSession``; the caller owns the transaction lifetime.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product
from bot.utils.avito_url import extract_avito_item_id

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[0-9a-zа-яё\-]{3,}", re.IGNORECASE)
# Words that carry no signal in vision/whisper output
_STOP_WORDS = frozenset(
    "это как что для или его она они так уже при без над под изображен изображена "
    "изображено изображены фото фотографии товар товара цвет цвета материал "
    "который которая которое есть также очень можно the and with this that".split()
)


def like_pattern(text: str) -> str:
    """Case-folded LIKE pattern with ``%``/``_``/``\\`` escaped (escape char ``\\``)."""
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def text_match(pattern: str):  # type: ignore[no-untyped-def]
    return or_(
        func.lower(Product.name).like(pattern, escape="\\"),
        func.lower(Product.description).like(pattern, escape="\\"),
        func.lower(Product.sku).like(pattern, escape="\\"),
    )


async def search_by_sku(
    session: AsyncSession,
    seller_id: int,
    sku: str,
) -> Product | None:
    """Return the product whose SKU matches *sku* for the given seller, or ``None``.

    SKU is not unique in the schema, so the newest match wins instead of raising.
    """
    result = await session.execute(
        select(Product)
        .where(Product.seller_id == seller_id, Product.sku == sku)
        .order_by(Product.id.desc())
        .limit(1)
    )
    product = result.scalars().first()
    logger.info(
        "search_by_sku seller=%d sku=%s -> %s",
        seller_id,
        sku,
        "found" if product else "not_found",
    )
    return product


async def search_by_avito_url(
    session: AsyncSession,
    seller_id: int,
    url: str,
) -> Product | None:
    """Return the product whose ``avito_item_id`` is embedded in *url*, or ``None``.

    Returns ``None`` immediately (without hitting the DB) when *url* contains
    no parseable Avito item ID.
    """
    item_id = extract_avito_item_id(url)
    if item_id is None:
        logger.info(
            "search_by_avito_url seller=%d url=%r -> not_found (no item_id parsed)",
            seller_id,
            url,
        )
        return None

    result = await session.execute(
        select(Product)
        .where(Product.seller_id == seller_id, Product.avito_item_id == item_id)
        .order_by(Product.id.desc())
        .limit(1)
    )
    product = result.scalars().first()
    logger.info(
        "search_by_avito_url seller=%d item_id=%s -> %s",
        seller_id,
        item_id,
        "found" if product else "not_found",
    )
    return product


async def search_by_text(
    session: AsyncSession,
    seller_id: int,
    query: str,
    limit: int = 3,
) -> list[Product]:
    """Return up to *limit* products that contain *query* in name, description, or SKU.

    Matching is case-insensitive (``func.lower()`` + ``like()``); LIKE
    wildcards in *query* are matched literally.
    """
    result = await session.execute(
        select(Product)
        .where(Product.seller_id == seller_id, text_match(like_pattern(query)))
        .order_by(Product.created_at.desc())
        .limit(limit)
    )
    products = list(result.scalars().all())
    logger.info(
        "search_by_text seller=%d query=%r -> %d results",
        seller_id,
        query,
        len(products),
    )
    return products


def extract_keywords(text: str, max_words: int = 8) -> list[str]:
    """Pick distinct, meaningful words from free-form text (vision/whisper output)."""
    seen: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        word = word.strip("-")
        if len(word) < 3 or word in _STOP_WORDS or word in seen:
            continue
        seen.append(word)
        if len(seen) >= max_words:
            break
    return seen


async def search_by_keywords(
    session: AsyncSession,
    seller_id: int,
    text: str,
    limit: int = 3,
) -> list[Product]:
    """Match any keyword from *text*; rank products by how many keywords they contain.

    Used for AI search where the query is a sentence, not a substring.
    """
    keywords = extract_keywords(text)
    if not keywords:
        return []

    result = await session.execute(
        select(Product)
        .where(
            Product.seller_id == seller_id,
            or_(*(text_match(like_pattern(k)) for k in keywords)),
        )
        .limit(200)
    )
    candidates = list(result.scalars().all())

    def score(p: Product) -> int:
        haystack = " ".join(filter(None, (p.name, p.description, p.sku))).lower()
        # Name hits weigh double: names are short and specific
        return sum((k in haystack) + (k in p.name.lower()) for k in keywords)

    candidates.sort(key=lambda p: (score(p), p.id), reverse=True)
    products = candidates[:limit]
    logger.info(
        "search_by_keywords seller=%d keywords=%r -> %d results",
        seller_id,
        keywords,
        len(products),
    )
    return products
