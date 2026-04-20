"""Search Telegram handlers: /search command and free-text message fallback."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Seller
from bot.services.search_service import search_by_avito_url, search_by_sku, search_by_text

logger = logging.getLogger(__name__)

router = Router(name="search")


# ---------------------------------------------------------------------------
# Internal helper (mirrors bot/handlers/products.py)
# ---------------------------------------------------------------------------


async def _get_seller(session: AsyncSession, telegram_id: int) -> Seller | None:
    """Return the Seller row for *telegram_id*, or None if not registered."""
    result = await session.execute(
        select(Seller).where(Seller.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Helper: format a single product card
# ---------------------------------------------------------------------------


def _format_product_card(product) -> str:  # type: ignore[no-untyped-def]
    lines = [f"📦 <b>{product.name}</b>"]
    lines.append(f"Кол-во: {product.quantity}")
    if product.sku:
        lines.append(f"Артикул: {product.sku}")
    if product.avito_url:
        lines.append(f"Avito: {product.avito_url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /search command handler
# ---------------------------------------------------------------------------


@router.message(Command("search"))
async def cmd_search(message: Message, session: AsyncSession) -> None:
    seller = await _get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    # Parse the argument — everything after "/search "
    text = (message.text or "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /search <артикул|ссылка Avito|текст>")
        return

    query = parts[1].strip()
    logger.info("Search query=%r seller=%d", query, seller.id)

    product = None

    # 1. Try Avito URL first
    if query.startswith("http"):
        product = await search_by_avito_url(session, seller.id, query)
        if product:
            await message.answer(_format_product_card(product), parse_mode="HTML")
            return

    # 2. Try exact SKU match
    product = await search_by_sku(session, seller.id, query)
    if product:
        await message.answer(_format_product_card(product), parse_mode="HTML")
        return

    # 3. Fall back to free-text search
    products = await search_by_text(session, seller.id, query, limit=3)
    if not products:
        await message.answer("Не найдено.")
        return

    lines = []
    for p in products:
        lines.append(_format_product_card(p))
    await message.answer("\n\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Free-text message handler (StateFilter(None) only, non-command messages)
# ---------------------------------------------------------------------------


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def msg_free_text_search(message: Message, session: AsyncSession) -> None:
    seller = await _get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    query = (message.text or "").strip()
    logger.info("Search text=%r seller=%d", query, seller.id)

    products = await search_by_text(session, seller.id, query, limit=3)
    if not products:
        await message.answer("Ничего не найдено.")
        return

    lines = []
    for p in products:
        lines.append(_format_product_card(p))
    await message.answer("\n\n".join(lines), parse_mode="HTML")
