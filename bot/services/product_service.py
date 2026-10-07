"""Product CRUD service — Telegram-free, operates on AsyncSession directly.

All mutating functions call ``session.flush()`` (not ``commit()``) so the
caller's unit-of-work transaction remains open; the handler layer commits.
"""
from __future__ import annotations

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product
from bot.services.search_service import like_pattern, text_match
from bot.utils.avito_url import extract_avito_item_id

LOW_STOCK = 2  # quantity at or below this (but > 0) counts as "low"


async def create_product(
    session: AsyncSession,
    seller_id: int,
    name: str,
    *,
    description: str | None = None,
    sku: str | None = None,
    quantity: int = 0,
    photo_file_id: str | None = None,
    avito_url: str | None = None,
    avito_item_id: str | None = None,
) -> Product:
    """Insert a new product row and return the flushed ORM object."""
    if avito_url and avito_item_id is None:
        avito_item_id = extract_avito_item_id(avito_url)
    product = Product(
        seller_id=seller_id,
        name=name,
        description=description,
        sku=sku,
        quantity=quantity,
        photo_file_id=photo_file_id,
        avito_url=avito_url,
        avito_item_id=avito_item_id,
    )
    session.add(product)
    await session.flush()
    return product


async def count_products(
    session: AsyncSession,
    seller_id: int,
) -> int:
    """Return the total number of products for the given seller."""
    result = await session.execute(
        select(func.count()).where(Product.seller_id == seller_id)
    )
    return result.scalar_one()


async def get_products(
    session: AsyncSession,
    seller_id: int,
    offset: int = 0,
    limit: int = 10,
) -> list[Product]:
    """Return paginated products for the given seller, newest first."""
    result = await session.execute(
        select(Product)
        .where(Product.seller_id == seller_id)
        .order_by(Product.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_product(
    session: AsyncSession,
    product_id: int,
    seller_id: int,
) -> Product | None:
    """Return a single product, or None if it does not exist or belongs to a different seller."""
    result = await session.execute(
        select(Product).where(
            Product.id == product_id,
            Product.seller_id == seller_id,
        )
    )
    return result.scalar_one_or_none()


async def update_quantity(
    session: AsyncSession,
    product_id: int,
    seller_id: int,
    quantity: int,
) -> bool:
    """Set the quantity of a product.  Raises ValueError for negative values.

    Returns True when exactly one row was updated, False when the product
    does not exist or belongs to a different seller.
    """
    if quantity < 0:
        raise ValueError("quantity must be >= 0")

    result = await session.execute(
        update(Product)
        .where(Product.id == product_id, Product.seller_id == seller_id)
        .values(quantity=quantity)
    )
    return result.rowcount > 0


async def delete_product(
    session: AsyncSession,
    product_id: int,
    seller_id: int,
) -> bool:
    """Delete a product.

    Returns True when exactly one row was deleted, False when the product
    does not exist or belongs to a different seller (tenant safety).
    """
    result = await session.execute(
        delete(Product).where(
            Product.id == product_id,
            Product.seller_id == seller_id,
        )
    )
    return result.rowcount > 0


_EDITABLE = frozenset({"name", "description", "sku", "quantity", "avito_url", "photo_file_id"})


async def update_product(
    session: AsyncSession,
    product_id: int,
    seller_id: int,
    **fields: object,
) -> Product | None:
    """Patch editable fields of a product; keeps ``avito_item_id`` in sync with the URL.

    Returns the updated product, or None when it does not exist or belongs
    to a different seller.
    """
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    if "quantity" in fields and int(fields["quantity"]) < 0:  # type: ignore[call-overload]
        raise ValueError("quantity must be >= 0")

    product = await get_product(session, product_id, seller_id)
    if product is None:
        return None
    for key, value in fields.items():
        setattr(product, key, value)
    if "avito_url" in fields:
        product.avito_item_id = extract_avito_item_id(product.avito_url)
    await session.flush()
    return product


async def inventory_stats(session: AsyncSession, seller_id: int) -> dict[str, int]:
    """Aggregate numbers for the dashboard in a single query."""
    has_text = func.coalesce(func.length(Product.sku), 0) + func.coalesce(
        func.length(Product.description), 0
    )
    row = (
        await session.execute(
            select(
                func.count(Product.id),
                func.coalesce(func.sum(Product.quantity), 0),
                func.count(case((Product.quantity == 0, 1))),
                func.count(case((Product.quantity.between(1, LOW_STOCK), 1))),
                func.count(case((has_text > 0, 1))),
                func.count(Product.photo_file_id),
                func.count(Product.avito_item_id),
            ).where(Product.seller_id == seller_id)
        )
    ).one()
    keys = ("positions", "units", "depleted", "low", "searchable", "with_photo", "on_avito")
    return dict(zip(keys, (int(v) for v in row)))


async def list_products(
    session: AsyncSession,
    seller_id: int,
    *,
    query: str | None = None,
    stock: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Product], int]:
    """Filtered page of products + total count. *stock*: ``depleted`` | ``low`` | None."""
    conditions = [Product.seller_id == seller_id]
    if query:
        conditions.append(text_match(like_pattern(query)))
    if stock == "depleted":
        conditions.append(Product.quantity == 0)
    elif stock == "low":
        conditions.append(Product.quantity.between(1, LOW_STOCK))

    total = (await session.execute(select(func.count(Product.id)).where(*conditions))).scalar_one()
    result = await session.execute(
        select(Product)
        .where(*conditions)
        .order_by(Product.created_at.desc(), Product.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total)
