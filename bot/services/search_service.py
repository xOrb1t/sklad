"""Search service — query products by SKU, Avito URL, or free text.

All functions are read-only (SELECT) and operate directly on an
``AsyncSession``; the caller owns the transaction lifetime.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product
from bot.utils.avito_url import extract_avito_item_id

logger = logging.getLogger(__name__)


async def search_by_sku(
    session: AsyncSession,
    seller_id: int,
    sku: str,
) -> Product | None:
    """Return the product whose SKU matches *sku* for the given seller, or ``None``."""
    result = await session.execute(
        select(Product).where(
            Product.seller_id == seller_id,
            Product.sku == sku,
        )
    )
    product = result.scalar_one_or_none()
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
        select(Product).where(
            Product.seller_id == seller_id,
            Product.avito_item_id == item_id,
        )
    )
    product = result.scalar_one_or_none()
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
    """Return up to *limit* products that match *query* in name, description, or SKU.

    Matching is case-insensitive (``func.lower()`` + ``like()``) and
    Cyrillic-safe because SQLite/Postgres both lower-case Unicode properly
    with this approach.
    """
    pattern = f"%{query.lower()}%"
    result = await session.execute(
        select(Product)
        .where(
            Product.seller_id == seller_id,
            or_(
                func.lower(Product.name).like(pattern),
                func.lower(Product.description).like(pattern),
                func.lower(Product.sku).like(pattern),
            ),
        )
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
