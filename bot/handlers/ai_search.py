"""AI-powered search handlers: photo (vision model → structured attributes) and voice (Whisper)."""
from __future__ import annotations

import html
import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product, Seller
from bot.services.groq_service import GroqServiceError, extract_item, transcribe_audio
from bot.services.query_terms import query_from_item
from bot.services.search_service import search_by_sku, search_by_terms, search_by_text

logger = logging.getLogger(__name__)

router = Router(name="ai_search")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_seller(session: AsyncSession, telegram_id: int) -> Seller | None:
    """Return the Seller row for *telegram_id*, or None if not registered."""
    result = await session.execute(
        select(Seller).where(Seller.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


def _format_product_card(product: Product) -> str:
    lines = [f"📦 <b>{html.escape(product.name)}</b>"]
    lines.append(f"Кол-во: {product.quantity}")
    if product.sku:
        lines.append(f"Артикул: {html.escape(product.sku)}")
    if product.avito_url:
        lines.append(f"Avito: {html.escape(product.avito_url)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Photo handler
# ---------------------------------------------------------------------------


@router.message(F.photo)
async def handle_photo_search(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Extract structured attributes from the photo via the vision model and search by them.

    Order: exact style code (from the tag/box or caption) → weighted term
    search over brand / model / category / colours.
    """
    seller = await _get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    await message.answer("Обрабатываю...")

    photo = message.photo[-1]  # type: ignore[index]
    buf = BytesIO()
    await bot.download(photo, destination=buf)
    image_bytes = buf.getvalue()
    caption = message.caption or None

    try:
        item = await extract_item(image_bytes, caption=caption, seller_id=seller.id)
    except GroqServiceError:
        await message.answer("Сервис временно недоступен, попробуйте позже.")
        return

    query = query_from_item(item, caption=caption)
    if item.is_empty() and not query.groups:
        await message.answer("Не удалось распознать товар на фото. Попробуйте другой ракурс или подпишите фото.")
        return

    recognized = f"🔎 Распознано: {html.escape(item.summary())}" if item.summary() else ""

    products: list[Product] = []
    if query.style_code:
        exact = await search_by_sku(session, seller.id, query.style_code)
        if exact:
            products = [exact]
    if not products:
        products = await search_by_terms(session, seller.id, query, limit=3)

    logger.info(
        "ai_photo_search seller=%d item=%s -> %d results",
        seller.id,
        item.model_dump(exclude_defaults=True),
        len(products),
    )

    if not products:
        text = "Ничего не найдено."
        if recognized:
            text = f"{recognized}\n\n{text}"
        await message.answer(text, parse_mode="HTML")
        return

    cards = "\n\n".join(_format_product_card(p) for p in products)
    await message.answer(f"{recognized}\n\n{cards}" if recognized else cards, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Voice handler
# ---------------------------------------------------------------------------


@router.message(F.voice)
async def handle_voice_search(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Transcribe the voice message via Groq Whisper and search for matching products."""
    seller = await _get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    await message.answer("Обрабатываю...")

    voice = message.voice  # type: ignore[union-attr]
    buf = BytesIO()
    await bot.download(voice, destination=buf)
    audio_bytes = buf.getvalue()

    try:
        transcript = await transcribe_audio(audio_bytes, seller_id=seller.id)
    except GroqServiceError:
        await message.answer("Сервис временно недоступен, попробуйте позже.")
        return

    if not transcript.strip():
        await message.answer("Ничего не найдено.")
        return

    products = await search_by_text(session, seller.id, transcript, limit=3)
    logger.info(
        "ai_voice_search seller=%d transcript=%r -> %d results",
        seller.id,
        transcript,
        len(products),
    )

    if not products:
        await message.answer("Ничего не найдено.")
        return

    lines = [_format_product_card(p) for p in products]
    await message.answer("\n\n".join(lines), parse_mode="HTML")
