"""End-to-end bot flows: real Dispatcher + routers + middleware, fake Bot API session.

These catch handler-ordering and filter bugs that calling handlers directly misses.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.middlewares import DbSessionMiddleware
from bot.models import Base, Product
from bot.routers import register_routers

USER = User(id=777, is_bot=False, first_name="T", username="tester")
CHAT = Chat(id=777, type="private")
_ids = itertools.count(1)


class FakeSession(BaseSession):
    """Records outgoing Bot API calls and returns minimal valid results."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(
                message_id=next(_ids),
                date=datetime.now(timezone.utc),
                chat=CHAT,
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
            )
        return True

    async def close(self) -> None:  # pragma: no cover - nothing to close
        pass

    async def stream_content(self, *a: Any, **kw: Any):  # type: ignore[no-untyped-def]  # pragma: no cover
        yield b""

    def texts(self) -> list[str]:
        out = []
        for m in self.calls:
            if isinstance(m, SendMessage):
                out.append(m.text)
            elif isinstance(m, SendPhoto):
                out.append(m.caption or "")
        return out

    def last_text(self) -> str:
        return self.texts()[-1]


# Routers are module-level singletons and can be attached to one Dispatcher
# only, so build it once and swap the DB behind the middleware per test.
_db_middleware = DbSessionMiddleware(None)  # type: ignore[arg-type]
_dp = Dispatcher()
_dp.update.middleware(_db_middleware)
register_routers(_dp)


@pytest_asyncio.fixture
async def env():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    _db_middleware.session_factory = factory

    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    # FSM storage is per-dispatcher: reset between tests
    _dp.fsm.storage = type(_dp.fsm.storage)()
    yield _dp, bot, session, factory
    await engine.dispose()


async def send(dp: Dispatcher, bot: Bot, text: str | None = None, photo: str | None = None) -> None:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=CHAT,
        from_user=USER,
        text=text,
        photo=[PhotoSize(file_id=photo, file_unique_id=photo, width=10, height=10)] if photo else None,
    )
    await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))


async def press(dp: Dispatcher, bot: Bot, data: str) -> None:
    anchor = Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=CHAT, text="x")
    cq = CallbackQuery(id=str(next(_ids)), from_user=USER, chat_instance="c", message=anchor, data=data)
    await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=cq))


async def products(factory: async_sessionmaker[AsyncSession]) -> list[Product]:
    async with factory() as s:
        return list((await s.execute(select(Product).order_by(Product.id))).scalars())


async def test_add_product_full_flow(env) -> None:  # type: ignore[no-untyped-def]
    dp, bot, api, factory = env
    await send(dp, bot, "/start")
    await send(dp, bot, "/addproduct")
    await send(dp, bot, photo="PHOTO1")
    await send(dp, bot, "Свечи <NGK>")
    await press(dp, bot, "skip")  # description
    await send(dp, bot, "NGK-97968")
    await send(dp, bot, "это не ссылка")
    assert "Не похоже" in api.last_text()
    await send(dp, bot, "https://www.avito.ru/moskva/zapchasti/svechi_ngk_3456789012")
    await send(dp, bot, "много")
    assert "целое число" in api.last_text()
    await send(dp, bot, "4")

    [p] = await products(factory)
    assert (p.name, p.description, p.sku, p.quantity) == ("Свечи <NGK>", None, "NGK-97968", 4)
    assert p.photo_file_id == "PHOTO1" and p.avito_item_id == "3456789012"
    assert any("&lt;NGK&gt;" in t for t in api.texts())


async def test_cancel_and_catch_all(env) -> None:  # type: ignore[no-untyped-def]
    dp, bot, api, factory = env
    await send(dp, bot, "/start")
    await send(dp, bot, "/addproduct")
    await send(dp, bot, "/search x")  # stray command inside the flow
    assert "Ожидаю" in api.last_text()
    await send(dp, bot, "/cancel")
    assert api.last_text() == "Отменено."
    await send(dp, bot, "просто текст")  # back to free-text search
    assert api.last_text() == "Ничего не найдено."
    assert await products(factory) == []


async def test_edit_fields_and_quantity(env) -> None:  # type: ignore[no-untyped-def]
    dp, bot, api, factory = env
    await send(dp, bot, "/start")
    await send(dp, bot, "/addproduct")
    await press(dp, bot, "skip")
    await send(dp, bot, "Фильтр")
    await send(dp, bot, "оригинал")
    await press(dp, bot, "skip")
    await press(dp, bot, "skip")
    await send(dp, bot, "10")
    [p] = await products(factory)

    await press(dp, bot, f"edit:{p.id}:quantity")
    await send(dp, bot, "-3")
    await press(dp, bot, f"edit:{p.id}:quantity")
    await send(dp, bot, "-100")
    assert "итог не меньше 0" in api.last_text()
    await send(dp, bot, "+5")
    await press(dp, bot, f"edit:{p.id}:description")
    await send(dp, bot, "-")
    await press(dp, bot, f"edit:{p.id}:sku")
    await send(dp, bot, "11428575211")
    await press(dp, bot, f"edit:{p.id}:photo")
    await send(dp, bot, photo="PHOTO2")
    await press(dp, bot, f"adj:{p.id}:1")

    [p] = await products(factory)
    assert (p.quantity, p.description, p.sku, p.photo_file_id) == (13, None, "11428575211", "PHOTO2")


async def test_other_seller_cannot_touch_product(env) -> None:  # type: ignore[no-untyped-def]
    dp, bot, api, factory = env
    async with factory() as s:
        from bot.models import Seller

        other = Seller(telegram_id=1)
        s.add(other)
        await s.flush()
        s.add(Product(seller_id=other.id, name="чужой", quantity=5))
        await s.commit()
    await send(dp, bot, "/start")
    [p] = await products(factory)
    await press(dp, bot, f"adj:{p.id}:-1")
    await press(dp, bot, f"delete_yes:{p.id}")
    [p] = await products(factory)
    assert p.quantity == 5
    alerts = [m for m in api.calls if isinstance(m, AnswerCallbackQuery) and m.text]
    assert alerts and alerts[0].text == "Товар не найден."
