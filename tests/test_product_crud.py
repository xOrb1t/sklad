"""Integration tests for product CRUD service.

Uses in-memory SQLite via aiosqlite — no Postgres or real Bot token needed.
Replicates the db_session fixture pattern from test_seller_registration.py.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Seller
from bot.services.product_service import (
    create_product,
    delete_product,
    get_product,
    get_products,
    update_quantity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Yield a fresh in-memory SQLite session with the full schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Helper: create a seller row (product requires a valid FK)
# ---------------------------------------------------------------------------

async def _make_seller(session: AsyncSession, telegram_id: int = 100) -> Seller:
    seller = Seller(telegram_id=telegram_id)
    session.add(seller)
    await session.flush()
    return seller


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_product(db_session: AsyncSession) -> None:
    """Created product receives a DB-assigned PK and the name is persisted."""
    seller = await _make_seller(db_session, telegram_id=1)
    product = await create_product(db_session, seller.id, name="Widget")

    assert product.id is not None, "flush() must populate PK"
    assert product.name == "Widget"
    assert product.seller_id == seller.id
    assert product.quantity == 0


@pytest.mark.asyncio
async def test_list_isolation(db_session: AsyncSession) -> None:
    """get_products returns only the calling seller's products."""
    seller_a = await _make_seller(db_session, telegram_id=2)
    seller_b = await _make_seller(db_session, telegram_id=3)

    await create_product(db_session, seller_a.id, name="A-product-1")
    await create_product(db_session, seller_a.id, name="A-product-2")
    await create_product(db_session, seller_b.id, name="B-product-1")

    products_a = await get_products(db_session, seller_a.id)
    products_b = await get_products(db_session, seller_b.id)

    assert len(products_a) == 2
    assert len(products_b) == 1


@pytest.mark.asyncio
async def test_get_product_cross_tenant(db_session: AsyncSession) -> None:
    """get_product with the wrong seller_id returns None (tenant safety)."""
    seller_a = await _make_seller(db_session, telegram_id=4)
    seller_b = await _make_seller(db_session, telegram_id=5)

    product_a = await create_product(db_session, seller_a.id, name="A's product")

    result = await get_product(db_session, product_a.id, seller_id=seller_b.id)
    assert result is None


@pytest.mark.asyncio
async def test_update_quantity(db_session: AsyncSession) -> None:
    """update_quantity returns True and the new quantity is persisted."""
    seller = await _make_seller(db_session, telegram_id=6)
    product = await create_product(db_session, seller.id, name="Updatable")

    updated = await update_quantity(db_session, product.id, seller.id, quantity=5)
    assert updated is True

    # Re-fetch to confirm persistence (ORM object may be stale after bulk UPDATE)
    fetched = await get_product(db_session, product.id, seller.id)
    assert fetched is not None
    assert fetched.quantity == 5


@pytest.mark.asyncio
async def test_update_quantity_negative(db_session: AsyncSession) -> None:
    """update_quantity raises ValueError for negative quantities."""
    seller = await _make_seller(db_session, telegram_id=7)
    product = await create_product(db_session, seller.id, name="Negative test")

    with pytest.raises(ValueError, match="quantity must be >= 0"):
        await update_quantity(db_session, product.id, seller.id, quantity=-1)


@pytest.mark.asyncio
async def test_delete_product(db_session: AsyncSession) -> None:
    """Deleting own product returns True; a second delete of the same product returns False."""
    seller = await _make_seller(db_session, telegram_id=8)
    product = await create_product(db_session, seller.id, name="Deletable")

    first = await delete_product(db_session, product.id, seller.id)
    assert first is True

    second = await delete_product(db_session, product.id, seller.id)
    assert second is False


@pytest.mark.asyncio
async def test_delete_cross_tenant(db_session: AsyncSession) -> None:
    """Seller B cannot delete seller A's product (cross-tenant delete returns False)."""
    seller_a = await _make_seller(db_session, telegram_id=9)
    seller_b = await _make_seller(db_session, telegram_id=10)

    product_a = await create_product(db_session, seller_a.id, name="A's protected")

    result = await delete_product(db_session, product_a.id, seller_id=seller_b.id)
    assert result is False

    # Confirm the product still exists for seller A
    still_there = await get_product(db_session, product_a.id, seller_a.id)
    assert still_there is not None


@pytest.mark.asyncio
async def test_pagination(db_session: AsyncSession) -> None:
    """get_products(offset=10, limit=10) on a 15-item table returns exactly 5 items."""
    seller = await _make_seller(db_session, telegram_id=11)

    for i in range(15):
        await create_product(db_session, seller.id, name=f"Product {i:02d}")

    page = await get_products(db_session, seller.id, offset=10, limit=10)
    assert len(page) == 5
