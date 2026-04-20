"""Integration tests for the search service.

Uses in-memory SQLite via aiosqlite — no Postgres or real Bot token needed.
The db_session fixture is a direct copy of the one used in test_product_crud.py.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Seller
from bot.services.product_service import create_product
from bot.services.search_service import (
    search_by_avito_url,
    search_by_sku,
    search_by_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Yield a fresh in-memory SQLite session with the full schema.

    Registers a Unicode-aware ``lower()`` function with every new SQLite
    connection so that Cyrillic (and other non-ASCII) text search works
    correctly in tests.  SQLite's built-in lower() is ASCII-only; the
    production database (Postgres) handles Unicode natively.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _register_unicode_lower(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
        dbapi_conn.create_function("lower", 1, lambda s: s.lower() if s else s)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _make_seller(session: AsyncSession, telegram_id: int) -> Seller:
    seller = Seller(telegram_id=telegram_id)
    session.add(seller)
    await session.flush()
    return seller


# ---------------------------------------------------------------------------
# SKU tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_by_sku_exact(db_session: AsyncSession) -> None:
    """Product with sku='SKU-001' is found by exact SKU lookup."""
    seller = await _make_seller(db_session, telegram_id=1)
    await create_product(db_session, seller.id, name="Widget", sku="SKU-001")

    result = await search_by_sku(db_session, seller.id, "SKU-001")

    assert result is not None
    assert result.sku == "SKU-001"


@pytest.mark.asyncio
async def test_search_by_sku_not_found(db_session: AsyncSession) -> None:
    """Wrong SKU returns None — no crash."""
    seller = await _make_seller(db_session, telegram_id=2)
    await create_product(db_session, seller.id, name="Widget", sku="SKU-001")

    result = await search_by_sku(db_session, seller.id, "WRONG-SKU")

    assert result is None


@pytest.mark.asyncio
async def test_search_by_sku_cross_tenant(db_session: AsyncSession) -> None:
    """Seller B cannot find seller A's product by SKU (tenant isolation)."""
    seller_a = await _make_seller(db_session, telegram_id=3)
    seller_b = await _make_seller(db_session, telegram_id=4)
    await create_product(db_session, seller_a.id, name="A Widget", sku="SKU-TENANT-A")

    result = await search_by_sku(db_session, seller_b.id, "SKU-TENANT-A")

    assert result is None


# ---------------------------------------------------------------------------
# Avito URL tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_by_avito_url_found(db_session: AsyncSession) -> None:
    """Product with avito_item_id='3456789012' is found via full Avito URL."""
    seller = await _make_seller(db_session, telegram_id=5)
    await create_product(
        db_session,
        seller.id,
        name="iPhone",
        avito_item_id="3456789012",
    )

    result = await search_by_avito_url(
        db_session,
        seller.id,
        "https://www.avito.ru/moskva/telefony/iphone-3456789012",
    )

    assert result is not None
    assert result.avito_item_id == "3456789012"


@pytest.mark.asyncio
async def test_search_by_avito_url_invalid(db_session: AsyncSession) -> None:
    """Malformed URL returns None without raising an exception."""
    seller = await _make_seller(db_session, telegram_id=6)

    result = await search_by_avito_url(db_session, seller.id, "not-a-url")

    assert result is None


# ---------------------------------------------------------------------------
# Text search tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_by_text_name_match(db_session: AsyncSession) -> None:
    """Product with name 'Красный кроссовок' is found by query 'красный'."""
    seller = await _make_seller(db_session, telegram_id=7)
    await create_product(db_session, seller.id, name="Красный кроссовок")

    results = await search_by_text(db_session, seller.id, "красный")

    assert len(results) == 1
    assert results[0].name == "Красный кроссовок"


@pytest.mark.asyncio
async def test_search_by_text_description_match(db_session: AsyncSession) -> None:
    """Text search hits on the description field."""
    seller = await _make_seller(db_session, telegram_id=8)
    await create_product(
        db_session,
        seller.id,
        name="Generic Product",
        description="Удобные зимние ботинки из натуральной кожи",
    )

    results = await search_by_text(db_session, seller.id, "зимние")

    assert len(results) == 1
    assert "зимние" in (results[0].description or "").lower()


@pytest.mark.asyncio
async def test_search_by_text_no_results(db_session: AsyncSession) -> None:
    """Query that matches nothing returns an empty list."""
    seller = await _make_seller(db_session, telegram_id=9)
    await create_product(db_session, seller.id, name="Красный кроссовок")

    results = await search_by_text(db_session, seller.id, "велосипед")

    assert results == []


@pytest.mark.asyncio
async def test_search_by_text_cross_tenant(db_session: AsyncSession) -> None:
    """Text search is scoped to the caller's seller_id (tenant isolation)."""
    seller_a = await _make_seller(db_session, telegram_id=10)
    seller_b = await _make_seller(db_session, telegram_id=11)
    await create_product(db_session, seller_b.id, name="Синяя куртка")

    # Seller A searches for a term that only appears in seller B's catalogue
    results = await search_by_text(db_session, seller_a.id, "синяя")

    assert results == []
