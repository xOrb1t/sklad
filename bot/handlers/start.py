import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Seller

logger = logging.getLogger(__name__)

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession) -> None:
    """Register a new seller on /start, or greet a returning one (idempotent)."""
    result = await session.execute(
        select(Seller).where(Seller.telegram_id == message.from_user.id)
    )
    seller = result.scalar_one_or_none()

    if seller is None:
        seller = Seller(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
        )
        session.add(seller)
        await session.flush()  # obtain PK before middleware commit
        logger.info("New seller registered (telegram_id omitted for privacy)")
        await message.answer("Добро пожаловать! Вы зарегистрированы как продавец.")
    else:
        await message.answer("С возвращением!")
