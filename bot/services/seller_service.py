"""Seller lookup helpers shared by Telegram handlers and the web panel."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Seller


async def get_seller(session: AsyncSession, telegram_id: int) -> Seller | None:
    """Return the Seller row for *telegram_id*, or None if not registered."""
    result = await session.execute(
        select(Seller).where(Seller.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def get_seller_by_id(session: AsyncSession, seller_id: int) -> Seller | None:
    return await session.get(Seller, seller_id)
