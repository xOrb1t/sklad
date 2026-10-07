"""Search Telegram handlers: /search command and free-text message fallback."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.formatting import format_product_card
from bot.services.search_service import search_by_avito_url, search_by_sku, search_by_text
from bot.services.seller_service import get_seller

logger = logging.getLogger(__name__)

router = Router(name="search")


@router.message(Command("search"))
async def cmd_search(message: Message, session: AsyncSession) -> None:
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    # Parse the argument — everything after "/search "
    text = (message.text or "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /search &lt;артикул|ссылка Avito|текст&gt;")
        return

    query = parts[1].strip()
    logger.info("Search query=%r seller=%d", query, seller.id)

    # 1. Try Avito URL first
    if query.startswith("http"):
        product = await search_by_avito_url(session, seller.id, query)
        if product:
            await message.answer(format_product_card(product), parse_mode="HTML")
            return

    # 2. Try exact SKU match
    product = await search_by_sku(session, seller.id, query)
    if product:
        await message.answer(format_product_card(product), parse_mode="HTML")
        return

    # 3. Fall back to free-text search
    products = await search_by_text(session, seller.id, query, limit=3)
    if not products:
        await message.answer("Не найдено.")
        return

    await message.answer("\n\n".join(format_product_card(p) for p in products), parse_mode="HTML")


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def msg_free_text_search(message: Message, session: AsyncSession) -> None:
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    query = (message.text or "").strip()
    logger.info("Search text=%r seller=%d", query, seller.id)

    products = await search_by_text(session, seller.id, query, limit=3)
    if not products:
        await message.answer("Ничего не найдено.")
        return

    await message.answer("\n\n".join(format_product_card(p) for p in products), parse_mode="HTML")
