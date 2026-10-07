"""Unit tests for query normalisation and weighted term search."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Seller
from bot.services.product_service import create_product
from bot.services.query_terms import (
    ExtractedItem,
    parse_extraction,
    query_from_item,
    query_from_text,
    stem,
)
from bot.services.search_service import search_by_terms, search_by_text


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
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


async def _make_seller(session: AsyncSession, telegram_id: int) -> Seller:
    seller = Seller(telegram_id=telegram_id)
    session.add(seller)
    await session.flush()
    return seller


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def test_stem_strips_russian_endings() -> None:
    assert stem("кроссовки") == "кроссов"
    assert stem("зимние") == "зимн"
    assert stem("nike") == "nike"
    assert stem("худи") == "худи"


def test_query_from_text_dictionaries() -> None:
    q = query_from_text("есть ли чёрные найки форсы?")
    kinds = {g.kind for g in q.groups}
    assert {"brand", "model", "color"} <= kinds
    # dictionary hits are not duplicated as plain words, stopwords are dropped
    assert not any(g.kind == "word" for g in q.groups)


def test_query_from_text_style_code() -> None:
    q = query_from_text("нужен cw2288-111 в 42")
    assert q.style_code == "CW2288-111"
    assert q.groups[0].kind == "style_code"


def test_query_from_item_brand_aliases() -> None:
    item = ExtractedItem(brand="New Balance", model="550", category="sneakers", colors=["white", "green"])
    q = query_from_item(item, caption="42 размер")
    brand = next(g for g in q.groups if g.kind == "brand")
    assert "нью беланс" in brand.alternatives
    assert any(g.kind == "category" for g in q.groups)
    assert sum(g.kind == "color" for g in q.groups) == 2


def test_parse_extraction_tolerates_nulls() -> None:
    item = parse_extraction('{"brand": null, "colors": null, "visible_text": "NIKE"}')
    assert item.colors == []
    assert item.visible_text == ["NIKE"]
    assert item.is_empty()


def test_parse_extraction_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_extraction("no json here")


# ---------------------------------------------------------------------------
# DB-backed ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_by_terms_ranks_brand_over_color(db_session: AsyncSession) -> None:
    seller = await _make_seller(db_session, 1)
    await create_product(db_session, seller.id, name="Черная куртка")
    target = await create_product(db_session, seller.id, name="Кроссовки Найк Air Force черные")
    await create_product(db_session, seller.id, name="Adidas Samba белые")

    item = ExtractedItem(brand="Nike", model="Air Force 1", category="sneakers", colors=["black"])
    results = await search_by_terms(db_session, seller.id, query_from_item(item))

    assert results[0].id == target.id
    assert all("Samba" not in p.name for p in results)


@pytest.mark.asyncio
async def test_search_by_terms_tenant_isolation(db_session: AsyncSession) -> None:
    a = await _make_seller(db_session, 2)
    b = await _make_seller(db_session, 3)
    await create_product(db_session, b.id, name="Nike Dunk")

    results = await search_by_terms(db_session, a.id, query_from_text("nike dunk"))
    assert results == []


@pytest.mark.asyncio
async def test_search_by_text_falls_back_to_terms(db_session: AsyncSession) -> None:
    """A conversational phrase finds the product even though it is not a substring."""
    seller = await _make_seller(db_session, 4)
    await create_product(db_session, seller.id, name="Худи Stone Island серое")
    await create_product(db_session, seller.id, name="Футболка белая")

    results = await search_by_text(db_session, seller.id, "покажи серые худи стоник")
    assert [p.name for p in results][0] == "Худи Stone Island серое"


@pytest.mark.asyncio
async def test_search_by_terms_escapes_like_wildcards(db_session: AsyncSession) -> None:
    seller = await _make_seller(db_session, 5)
    await create_product(db_session, seller.id, name="abc")

    from bot.services.query_terms import SearchQuery, TermGroup

    q = SearchQuery(groups=[TermGroup(("%",))])
    assert await search_by_terms(db_session, seller.id, q) == []
