"""Search service — query products by SKU, Avito URL, or free text.

All functions are read-only (SELECT) and operate directly on an
``AsyncSession``; the caller owns the transaction lifetime.
"""
from __future__ import annotations

import logging

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product
from bot.services.query_terms import SearchQuery, query_from_text
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

    First tries the whole query as one substring (precise for short queries
    like «красный»). If that finds nothing, falls back to weighted term
    matching (:func:`search_by_terms`) so that phrases such as
    «есть ли чёрные найки» still hit «Nike Air Force черные».

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
    if not products:
        products = await search_by_terms(session, seller_id, query_from_text(query), limit)
    logger.info(
        "search_by_text seller=%d query=%r -> %d results",
        seller_id,
        query,
        len(products),
    )
    return products


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_by_terms(
    session: AsyncSession,
    seller_id: int,
    query: SearchQuery,
    limit: int = 3,
) -> list[Product]:
    """Rank products by the summed weight of term groups they contain.

    Each :class:`~bot.services.query_terms.TermGroup` contributes its weight
    once if any of its alternatives is a substring of the product's name,
    description or SKU. Products with zero score are excluded. Ties are broken
    by newest first.
    """
    if not query.groups:
        return []

    haystack = (
        func.lower(func.coalesce(Product.name, ""))
        + literal(" ")
        + func.lower(func.coalesce(Product.description, ""))
        + literal(" ")
        + func.lower(func.coalesce(Product.sku, ""))
    )
    score_terms = [
        case(
            (
                or_(*(haystack.like(f"%{_escape_like(a)}%", escape="\\") for a in g.alternatives)),
                g.weight,
            ),
            else_=0,
        )
        for g in query.groups
    ]
    score = sum(score_terms[1:], score_terms[0]).label("score")

    result = await session.execute(
        select(Product, score)
        .where(Product.seller_id == seller_id, score > 0)
        .order_by(score.desc(), Product.id.desc())
        .limit(limit)
    )
    rows = result.all()
    logger.info(
        "search_by_terms seller=%d groups=%d -> %s",
        seller_id,
        len(query.groups),
        [(p.id, sc) for p, sc in rows],
    )
    return [p for p, _ in rows]
