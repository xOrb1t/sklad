"""Regression tests for review fixes: FSM routing, HTML escaping, search, tokens."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.formatting import format_product_card
from bot.models import Base, Seller
from bot.services import web_auth
from bot.services.product_service import create_product, inventory_stats, list_products, update_product
from bot.services.search_service import (
    extract_keywords,
    search_by_avito_url,
    search_by_keywords,
    search_by_sku,
    search_by_text,
)
from bot.utils.avito_url import extract_avito_item_id


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _lower(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.create_function("lower", 1, lambda s: s.lower() if s else s)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seller(session: AsyncSession, tg: int = 1) -> Seller:
    s = Seller(telegram_id=tg)
    session.add(s)
    await session.flush()
    return s


# --------------------------------------------------------------------- routing


def test_fsm_catch_all_registered_last() -> None:
    """aiogram matches in registration order: the catch-all must not shadow FSM steps."""
    from bot.handlers.products import router

    names = [h.callback.__name__ for h in router.message.handlers]
    assert names[-1] == "fsm_catch_all"
    assert names.index("fsm_name") < names.index("fsm_catch_all")


# --------------------------------------------------------------------- formatting


def test_card_escapes_html() -> None:
    class P:
        name = "<b>x</b> & co"
        quantity = 1
        description = "<script>"
        sku = "a<b"
        avito_url = None

    text = format_product_card(P(), with_description=True)  # type: ignore[arg-type]
    assert "<b>x</b> &" not in text
    assert "&lt;b&gt;x&lt;/b&gt; &amp; co" in text
    assert "&lt;script&gt;" in text and "a&lt;b" in text


# --------------------------------------------------------------------- avito


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.avito.ru/moskva/telefony/iphone_15_pro_3456789012", "3456789012"),
        ("https://www.avito.ru/moskva/telefony/iphone_15_pro_3456789012?context=abc", "3456789012"),
        ("https://www.avito.ru/moskva/telefony/iphone-3456789012", "3456789012"),
        ("https://www.avito.ru/moskva/telefony", None),
    ],
)
def test_extract_avito_item_id(url: str, expected: str | None) -> None:
    assert extract_avito_item_id(url) == expected


async def test_create_product_derives_avito_id(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    url = "https://www.avito.ru/moskva/audio/sony_xm5_4123456789"
    await create_product(db_session, s.id, "Sony", avito_url=url)
    assert (await search_by_avito_url(db_session, s.id, url)) is not None


# --------------------------------------------------------------------- search


async def test_duplicate_sku_does_not_raise(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    await create_product(db_session, s.id, "A", sku="DUP")
    newer = await create_product(db_session, s.id, "B", sku="DUP")
    found = await search_by_sku(db_session, s.id, "DUP")
    assert found is not None and found.id == newer.id


async def test_like_wildcards_are_literal(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    await create_product(db_session, s.id, "Скидка 50% на всё")
    await create_product(db_session, s.id, "Обычный товар")
    assert [p.name for p in await search_by_text(db_session, s.id, "%")] == ["Скидка 50% на всё"]
    assert await search_by_text(db_session, s.id, "_") == []


def test_extract_keywords_drops_noise() -> None:
    assert extract_keywords("На фото изображены красные кроссовки Nike, кожа") == [
        "красные", "кроссовки", "nike", "кожа",
    ]


async def test_search_by_keywords_ranks_best_match(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    await create_product(db_session, s.id, "Красная кружка")
    best = await create_product(db_session, s.id, "Кроссовки Nike", description="красные, кожа")
    await create_product(db_session, s.id, "Термос")
    res = await search_by_keywords(db_session, s.id, "красные кроссовки nike из кожи")
    assert res[0].id == best.id
    assert all(p.name != "Термос" for p in res)


# --------------------------------------------------------------------- services


async def test_update_product_and_stats(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    p = await create_product(db_session, s.id, "X", quantity=5)
    await create_product(db_session, s.id, "Y", quantity=0, sku="Y1")
    await create_product(db_session, s.id, "Z", quantity=1)

    upd = await update_product(db_session, p.id, s.id, avito_url="https://www.avito.ru/a/b/x_1234567")
    assert upd is not None and upd.avito_item_id == "1234567"
    with pytest.raises(ValueError):
        await update_product(db_session, p.id, s.id, avito_item_id="x")
    assert await update_product(db_session, p.id, s.id + 1, name="hack") is None

    st = await inventory_stats(db_session, s.id)
    assert st["positions"] == 3 and st["units"] == 6
    assert st["depleted"] == 1 and st["low"] == 1 and st["on_avito"] == 1 and st["searchable"] == 1

    items, total = await list_products(db_session, s.id, stock="depleted")
    assert total == 1 and items[0].name == "Y"


# --------------------------------------------------------------------- tokens


def test_web_tokens() -> None:
    tok = web_auth.issue(7, web_auth.LOGIN, ttl=60, now=1000)
    assert web_auth.verify(tok, web_auth.LOGIN, now=1030) == 7
    assert web_auth.verify(tok, web_auth.LOGIN, now=1061) is None  # expired
    assert web_auth.verify(tok, web_auth.SESSION, now=1030) is None  # wrong purpose
    forged = tok.replace("l.7.", "l.8.")
    assert web_auth.verify(forged, web_auth.LOGIN, now=1030) is None
    assert web_auth.verify("garbage", web_auth.LOGIN) is None
