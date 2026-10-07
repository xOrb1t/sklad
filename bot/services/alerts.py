"""Low-stock / out-of-stock Telegram alerts.

Sent for changes made outside the chat (web panel, CSV import).  In the bot
itself the product card already shows the stock state, so no extra message.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot

from bot.models import Seller
from bot.services.product_service import StockChange

logger = logging.getLogger(__name__)

MAX_LINES = 20


def build_alert(seller: Seller, changes: list[StockChange]) -> str | None:
    """Text for the changes that crossed into low/out, or None when nothing did."""
    if not seller.notify_low_stock:
        return None
    lines: list[str] = []
    for ch in changes:
        level = ch.alert(seller.low_stock_threshold)
        if level == "out":
            lines.append(f"❌ <b>{escape(ch.product.name)}</b> — закончился")
        elif level == "low":
            lines.append(f"⚠️ <b>{escape(ch.product.name)}</b> — осталось {ch.new} шт.")
    if not lines:
        return None
    extra = len(lines) - MAX_LINES
    body = "\n".join(lines[:MAX_LINES]) + (f"\n…и ещё {extra}" if extra > 0 else "")
    return "Остатки на складе:\n" + body


async def send_stock_alerts(bot: Bot, seller: Seller, changes: list[StockChange]) -> None:
    text = build_alert(seller, changes)
    if text is None:
        return
    try:
        await bot.send_message(seller.telegram_id, text, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001 — an alert must never break the request
        logger.warning("stock alert failed seller=%d: %s", seller.id, e)
