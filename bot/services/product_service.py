"""Product CRUD service — Telegram-free, operates on AsyncSession directly.

All mutating functions call ``session.flush()`` (not ``commit()``) so the
caller's unit-of-work transaction remains open; the handler layer commits.

Every quantity change goes through :func:`set_quantity`, which writes a
``StockMovement`` row — keep it that way so the history stays complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product, StockMovement
from bot.services.search_service import like_pattern, text_match
from bot.utils.avito_url import extract_avito_item_id

DEFAULT_LOW_STOCK = 2  # used when the caller has no seller-specific threshold

Source = Literal["bot", "web", "import"]
StockLevel = Literal["ok", "low", "out"]


def stock_level(quantity: int, threshold: int = DEFAULT_LOW_STOCK) -> StockLevel:
    if quantity <= 0:
        return "out"
    if quantity <= threshold:
        return "low"
    return "ok"


@dataclass(frozen=True)
class StockChange:
    product: Product
    old: int
    new: int

    def alert(self, threshold: int) -> StockLevel | None:
        """'low'/'out' when this change moved the product *into* that level, else None."""
        before, after = stock_level(self.old, threshold), stock_level(self.new, threshold)
        if after != "ok" and before != after and self.new < self.old:
            return after
        return None


async def set_quantity(
    session: AsyncSession, product: Product, quantity: int, source: Source
) -> StockChange | None:
    """Set quantity and log the movement. Returns None when nothing changed."""
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    old = product.quantity or 0
    if quantity == old and product.id is not None:
        return None
    product.quantity = quantity
    await session.flush()  # ensures product.id for brand-new rows
    session.add(StockMovement(product_id=product.id, delta=quantity - old, quantity_after=quantity, source=source))
    await session.flush()
    return StockChange(product, old, quantity)


async def adjust_quantity(
    session: AsyncSession, product: Product, delta: int, source: Source
) -> StockChange | None:
    """Add *delta* (clamped at 0)."""
    return await set_quantity(session, product, max(0, product.quantity + delta), source)


async def get_history(session: AsyncSession, product_id: int, seller_id: int, limit: int = 20) -> list[StockMovement]:
    result = await session.execute(
        select(StockMovement)
        .join(Product, Product.id == StockMovement.product_id)
        .where(StockMovement.product_id == product_id, Product.seller_id == seller_id)
        .order_by(StockMovement.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


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
    source: Source = "bot",
) -> Product:
    """Insert a new product row (initial stock is logged as a movement) and return it."""
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    if avito_url and avito_item_id is None:
        avito_item_id = extract_avito_item_id(avito_url)
    product = Product(
        seller_id=seller_id,
        name=name,
        description=description,
        sku=sku,
        quantity=0,
        photo_file_id=photo_file_id,
        avito_url=avito_url,
        avito_item_id=avito_item_id,
    )
    session.add(product)
    await session.flush()
    if quantity:
        await set_quantity(session, product, quantity, source)
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
        .order_by(Product.created_at.desc(), Product.id.desc())
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
    source: Source = "bot",
) -> bool:
    """Set the quantity of a product.  Raises ValueError for negative values.

    Returns True when the product exists for this seller, False otherwise.
    """
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    product = await get_product(session, product_id, seller_id)
    if product is None:
        return False
    await set_quantity(session, product, quantity, source)
    return True


async def delete_product(
    session: AsyncSession,
    product_id: int,
    seller_id: int,
) -> bool:
    """Delete a product.

    Returns True when exactly one row was deleted, False when the product
    does not exist or belongs to a different seller (tenant safety).
    """
    product = await get_product(session, product_id, seller_id)
    if product is None:
        return False
    # Explicit delete of the log: SQLite ignores ON DELETE CASCADE without PRAGMA foreign_keys
    await session.execute(delete(StockMovement).where(StockMovement.product_id == product_id))
    await session.delete(product)
    await session.flush()
    return True


_EDITABLE = frozenset({"name", "description", "sku", "quantity", "avito_url", "photo_file_id"})


async def update_product(
    session: AsyncSession,
    product_id: int,
    seller_id: int,
    *,
    source: Source = "bot",
    changes: list[StockChange] | None = None,
    **fields: object,
) -> Product | None:
    """Patch editable fields of a product; keeps ``avito_item_id`` in sync with the URL.

    A quantity change is logged; the resulting :class:`StockChange` is appended
    to *changes* when given (for alerts).  Returns None when the product does
    not exist or belongs to a different seller.
    """
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    quantity = fields.pop("quantity", None)
    if quantity is not None and int(quantity) < 0:  # type: ignore[call-overload]
        raise ValueError("quantity must be >= 0")

    product = await get_product(session, product_id, seller_id)
    if product is None:
        return None
    for key, value in fields.items():
        setattr(product, key, value)
    if "avito_url" in fields:
        product.avito_item_id = extract_avito_item_id(product.avito_url)
    if quantity is not None:
        change = await set_quantity(session, product, int(quantity), source)  # type: ignore[call-overload]
        if change and changes is not None:
            changes.append(change)
    await session.flush()
    return product


async def inventory_stats(
    session: AsyncSession, seller_id: int, low_stock: int = DEFAULT_LOW_STOCK
) -> dict[str, int]:
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
                func.count(case((Product.quantity.between(1, low_stock), 1))),
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
    low_stock: int = DEFAULT_LOW_STOCK,
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
        conditions.append(Product.quantity.between(1, low_stock))

    total = (await session.execute(select(func.count(Product.id)).where(*conditions))).scalar_one()
    result = await session.execute(
        select(Product)
        .where(*conditions)
        .order_by(Product.created_at.desc(), Product.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total)
