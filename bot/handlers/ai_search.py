"""AI-powered search handlers: photo (Groq Vision) and voice (Groq Whisper)."""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Seller
from bot.services.groq_service import GroqServiceError, describe_image, transcribe_audio
from bot.services.search_service import search_by_text

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


def _format_product_card(product) -> str:  # type: ignore[no-untyped-def]
    lines = [f"📦 <b>{product.name}</b>"]
    lines.append(f"Кол-во: {product.quantity}")
    if product.sku:
        lines.append(f"Артикул: {product.sku}")
    if product.avito_url:
        lines.append(f"Avito: {product.avito_url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Photo handler
# ---------------------------------------------------------------------------


@router.message(F.photo)
async def handle_photo_search(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Describe the photo via Groq Vision and search for matching products."""
    seller = await _get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    await message.answer("Обрабатываю...")

    photo = message.photo[-1]  # type: ignore[index]
    buf = BytesIO()
    await bot.download(photo, destination=buf)
    image_bytes = buf.getvalue()

    try:
        description = await describe_image(image_bytes, seller_id=seller.id)
    except GroqServiceError:
        await message.answer("Сервис временно недоступен, попробуйте позже.")
        return

    if not description.strip():
        await message.answer("Ничего не найдено.")
        return

    products = await search_by_text(session, seller.id, description, limit=3)
    logger.info(
        "ai_photo_search seller=%d description=%r -> %d results",
        seller.id,
        description,
        len(products),
    )

    if not products:
        await message.answer("Ничего не найдено.")
        return

    lines = [_format_product_card(p) for p in products]
    await message.answer("\n\n".join(lines), parse_mode="HTML")


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
