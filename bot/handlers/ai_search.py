"""AI-powered search handlers: photo (Groq Vision) and voice (Groq Whisper)."""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.formatting import format_product_card
from bot.services.groq_service import GroqServiceError, describe_image, transcribe_audio
from bot.services.search_service import search_by_keywords
from bot.services.seller_service import get_seller

logger = logging.getLogger(__name__)

router = Router(name="ai_search")


async def _reply_with_results(message: Message, session: AsyncSession, seller_id: int, text: str) -> None:
    products = await search_by_keywords(session, seller_id, text, limit=3)
    if not products:
        await message.answer("Ничего не найдено.")
        return
    await message.answer("\n\n".join(format_product_card(p) for p in products), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Photo handler
# ---------------------------------------------------------------------------


@router.message(StateFilter(None), F.photo)
async def handle_photo_search(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Describe the photo via Groq Vision and search for matching products."""
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    await message.answer("Обрабатываю...")

    photo = message.photo[-1]  # type: ignore[index]
    buf = BytesIO()
    await bot.download(photo, destination=buf)

    try:
        description = await describe_image(buf.getvalue(), seller_id=seller.id)
    except GroqServiceError:
        await message.answer("Сервис временно недоступен, попробуйте позже.")
        return

    if not description.strip():
        await message.answer("Ничего не найдено.")
        return

    logger.info("ai_photo_search seller=%d description=%r", seller.id, description)
    await _reply_with_results(message, session, seller.id, description)


# ---------------------------------------------------------------------------
# Voice handler
# ---------------------------------------------------------------------------


@router.message(StateFilter(None), F.voice)
async def handle_voice_search(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Transcribe the voice message via Groq Whisper and search for matching products."""
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала используйте /start")
        return

    await message.answer("Обрабатываю...")

    voice = message.voice  # type: ignore[union-attr]
    buf = BytesIO()
    await bot.download(voice, destination=buf)

    try:
        transcript = await transcribe_audio(buf.getvalue(), seller_id=seller.id)
    except GroqServiceError:
        await message.answer("Сервис временно недоступен, попробуйте позже.")
        return

    if not transcript.strip():
        await message.answer("Ничего не найдено.")
        return

    logger.info("ai_voice_search seller=%d transcript=%r", seller.id, transcript)
    await _reply_with_results(message, session, seller.id, transcript)
