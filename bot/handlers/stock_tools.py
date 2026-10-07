"""Stock tools: low-stock alert settings, CSV export/import via documents."""
from __future__ import annotations

import logging
import time
from html import escape
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import alerts_keyboard
from bot.models import Seller
from bot.services.alerts import build_alert
from bot.services.csv_service import MAX_BYTES, CsvImportError, export_csv, import_csv
from bot.services.product_service import list_products
from bot.services.seller_service import get_seller
from bot.states import AlertSettings

logger = logging.getLogger(__name__)

router = Router(name="stock_tools")

NOT_REGISTERED = "Сначала нажмите /start"


def _alerts_text(seller: Seller) -> str:
    state = "включены" if seller.notify_low_stock else "выключены"
    return (
        f"🔔 Уведомления об остатках: <b>{state}</b>\n"
        f"Порог «мало»: <b>{seller.low_stock_threshold}</b> шт.\n\n"
        "Сообщение придёт, когда после изменений в веб-панели или импорта "
        "остаток опустится до порога или до нуля."
    )


@router.message(Command("alerts"))
async def cmd_alerts(message: Message, session: AsyncSession) -> None:
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer(NOT_REGISTERED)
        return
    await message.answer(
        _alerts_text(seller), reply_markup=alerts_keyboard(seller.notify_low_stock, seller.low_stock_threshold)
    )


@router.callback_query(F.data == "alerts:toggle")
async def cb_alerts_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    seller = await get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.answer(NOT_REGISTERED, show_alert=True)
        return
    seller.notify_low_stock = not seller.notify_low_stock
    await session.flush()
    await callback.answer("Включено" if seller.notify_low_stock else "Выключено")
    await callback.message.edit_text(  # type: ignore[union-attr]
        _alerts_text(seller), reply_markup=alerts_keyboard(seller.notify_low_stock, seller.low_stock_threshold)
    )


@router.callback_query(F.data == "alerts:threshold")
async def cb_alerts_threshold(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AlertSettings.waiting_threshold)
    await callback.message.answer("Введите порог «мало» — целое число ≥ 0 (/cancel — отмена):")  # type: ignore[union-attr]


@router.message(AlertSettings.waiting_threshold, Command("cancel"))
async def cmd_cancel_threshold(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(AlertSettings.waiting_threshold, F.text, ~F.text.startswith("/"))
async def fsm_threshold(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) > 1_000_000:
        await message.answer("Нужно целое число ≥ 0:")
        return
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    await state.clear()
    if seller is None:
        await message.answer(NOT_REGISTERED)
        return
    seller.low_stock_threshold = int(text)
    await session.flush()
    await message.answer(
        _alerts_text(seller), reply_markup=alerts_keyboard(seller.notify_low_stock, seller.low_stock_threshold)
    )


@router.message(AlertSettings.waiting_threshold)
async def fsm_threshold_catch_all(message: Message) -> None:
    await message.answer("Ожидаю число. Введите его или /cancel для отмены.")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


@router.message(Command("export"))
async def cmd_export(message: Message, session: AsyncSession) -> None:
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer(NOT_REGISTERED)
        return
    items, total = await list_products(session, seller.id, offset=0, limit=1_000_000)
    if not total:
        await message.answer("Товаров пока нет.")
        return
    name = f"sklad-{time.strftime('%Y%m%d')}.csv"
    await message.answer_document(
        BufferedInputFile(export_csv(items), name),
        caption=f"{total} поз. Отредактируйте и пришлите файл обратно, чтобы обновить склад.",
    )


@router.message(StateFilter(None), F.document)
async def msg_import_document(message: Message, bot: Bot, session: AsyncSession) -> None:
    doc = message.document
    if doc is None or not (doc.file_name or "").lower().endswith(".csv"):
        await message.answer("Для импорта пришлите файл .csv (формат — как в /export).")
        return
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer(NOT_REGISTERED)
        return
    if doc.file_size and doc.file_size > MAX_BYTES:
        await message.answer("Файл больше 2 МБ.")
        return

    buf = BytesIO()
    await bot.download(doc, destination=buf)
    try:
        result = await import_csv(session, seller.id, buf.getvalue())
    except CsvImportError as e:
        shown = "\n".join(escape(x) for x in e.errors[:15])
        more = f"\n…и ещё {len(e.errors) - 15}" if len(e.errors) > 15 else ""
        await message.answer(f"❌ Импорт отменён, ничего не изменено.\n\n{shown}{more}")
        return

    logger.info("csv import seller=%d %s", seller.id, result.summary())
    text = f"✅ Импорт: {result.summary()}."
    alert = build_alert(seller, result.changes)
    if alert:
        text += "\n\n" + alert
    await message.answer(text)
