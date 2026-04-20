"""Integration tests for seller registration (get-or-create) logic.

Uses in-memory SQLite via aiosqlite — no Postgres or real Bot token needed.
Tests call the DB layer directly so there is no need to mock Telegram Bot API.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Seller


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
# Helper: reusable get-or-create logic (mirrors bot/handlers/start.py)
# ---------------------------------------------------------------------------

async def _get_or_create_seller(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
) -> tuple[Seller, bool]:
    """Return (seller, created). created=True when a new row was inserted."""
    result = await session.execute(
        select(Seller).where(Seller.telegram_id == telegram_id)
    )
    seller = result.scalar_one_or_none()
    if seller is None:
        seller = Seller(telegram_id=telegram_id, username=username)
        session.add(seller)
        await session.flush()
        return seller, True
    return seller, False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_seller_created(db_session: AsyncSession) -> None:
    """A brand-new telegram_id creates exactly one sellers row."""
    seller, created = await _get_or_create_seller(db_session, telegram_id=111_000_001)

    assert created is True
    assert seller.id is not None, "flush() must populate PK"
    assert seller.telegram_id == 111_000_001

    count_result = await db_session.execute(
        select(func.count()).select_from(Seller).where(Seller.telegram_id == 111_000_001)
    )
    assert count_result.scalar_one() == 1


@pytest.mark.asyncio
async def test_duplicate_start_no_duplicate(db_session: AsyncSession) -> None:
    """Calling get-or-create twice with the same telegram_id must not create a duplicate row."""
    tid = 222_000_002

    seller1, created1 = await _get_or_create_seller(db_session, telegram_id=tid)
    # commit so the second call sees the persisted row
    await db_session.commit()

    seller2, created2 = await _get_or_create_seller(db_session, telegram_id=tid)

    assert created1 is True
    assert created2 is False
    assert seller1.id == seller2.id, "Same seller must be returned on second call"

    count_result = await db_session.execute(
        select(func.count()).select_from(Seller).where(Seller.telegram_id == tid)
    )
    assert count_result.scalar_one() == 1, "Must have exactly one row after two /start calls"


@pytest.mark.asyncio
async def test_two_sellers_isolated(db_session: AsyncSession) -> None:
    """Two different telegram_ids produce two distinct seller rows with different PKs."""
    seller_a, _ = await _get_or_create_seller(db_session, telegram_id=333_000_001, username="alice")
    seller_b, _ = await _get_or_create_seller(db_session, telegram_id=333_000_002, username="bob")

    await db_session.commit()

    assert seller_a.id != seller_b.id, "Each seller must have a distinct PK"
    assert seller_a.telegram_id != seller_b.telegram_id

    # Verify both are stored in the DB
    all_result = await db_session.execute(
        select(Seller).where(
            Seller.telegram_id.in_([333_000_001, 333_000_002])
        )
    )
    sellers = all_result.scalars().all()
    assert len(sellers) == 2

    tids = {s.telegram_id for s in sellers}
    assert tids == {333_000_001, 333_000_002}
