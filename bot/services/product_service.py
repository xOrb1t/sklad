"""Product CRUD service — Telegram-free, operates on AsyncSession directly.

All mutating functions call ``session.flush()`` (not ``commit()``) so the
caller's unit-of-work transaction remains open; the handler layer commits.
"""
from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product


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
