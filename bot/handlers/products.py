"""Product CRUD Telegram handlers: /myproducts, /addproduct FSM, card actions, editing."""
from __future__ import annotations

import logging
import math
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.formatting import format_product_card
from bot.keyboards import (
    EDIT_FIELDS,
    confirm_delete_keyboard,
    edit_menu_keyboard,
    product_card_keyboard,
    product_list_keyboard,
    skip_keyboard,
)
from bot.models import Product
from bot.services.product_service import (
    count_products,
    create_product,
    delete_product,
    get_product,
    get_products,
    update_product,
)
from bot.services.seller_service import get_seller as _get_seller
from bot.states import AddProduct, EditProduct
from bot.utils.avito_url import extract_avito_item_id
from bot.utils.quantity import parse_quantity

logger = logging.getLogger(__name__)

router = Router(name="products")

PAGE_SIZE = 10

# Sent as a value to clear an optional field while editing
CLEAR_MARKERS = frozenset({"-", "—", "нет"})
NOT_REGISTERED = "Сначала нажмите /start"
QTY_PROMPT = "Введите количество: число (например 12) или изменение (+5 / -3):"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _send_product_list(
    target: Message | CallbackQuery,
    session: AsyncSession,
    seller_id: int,
    page: int = 0,
) -> None:
    """Fetch page and send (or edit) the product list message."""
    total = await count_products(session, seller_id)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))  # clamp

    products = await get_products(session, seller_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    keyboard = product_list_keyboard(products, page, total_pages)

    if not products and page == 0:
        text = "У вас ещё нет товаров. /addproduct чтобы добавить."
        if isinstance(target, Message):
            await target.answer(text)
        else:
            await target.message.answer(text)  # type: ignore[union-attr]
        return

    text = f"📦 Ваши товары (стр. {page + 1}/{total_pages}):"
    if isinstance(target, Message):
        await target.answer(text, reply_markup=keyboard)
    else:
        # CallbackQuery: try to edit the existing message
        try:
            await target.message.edit_text(text, reply_markup=keyboard)  # type: ignore[union-attr]
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=keyboard)  # type: ignore[union-attr]


async def _send_card(message: Message, product: Product) -> None:
    text = format_product_card(product, with_description=True)
    keyboard = product_card_keyboard(product.id)
    if product.photo_file_id:
        await message.answer_photo(
            product.photo_file_id, caption=text[:1024], parse_mode="HTML", reply_markup=keyboard
        )
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


def _parse_id(data: str | None, index: int = 1) -> int:
    return int((data or "").split(":")[index])


async def _start_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddProduct.waiting_photo)
    await message.answer(
        "Отправьте фото товара или нажмите «Пропустить» (/cancel — отмена):",
        reply_markup=skip_keyboard(),
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@router.message(Command("myproducts"))
async def cmd_myproducts(message: Message, session: AsyncSession) -> None:
    seller = await _get_seller(session, message.from_user.id)
    if seller is None:
        await message.answer(NOT_REGISTERED)
        return
    await _send_product_list(message, session, seller.id, page=0)


@router.message(Command("addproduct"))
async def cmd_addproduct(message: Message, session: AsyncSession, state: FSMContext) -> None:
    seller = await _get_seller(session, message.from_user.id)
    if seller is None:
        await message.answer(NOT_REGISTERED)
        return
    logger.info("Seller entered AddProduct FSM")
    await _start_add(message, state)


@router.message(StateFilter(AddProduct, EditProduct), Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    logger.info("Seller exited FSM via /cancel")
    await message.answer("Отменено.")


# ---------------------------------------------------------------------------
# FSM steps: AddProduct
# photo → name → description → sku → avito → quantity
# ---------------------------------------------------------------------------


@router.message(AddProduct.waiting_photo, F.photo)
async def fsm_photo_message(message: Message, state: FSMContext) -> None:
    await state.update_data(photo_file_id=message.photo[-1].file_id)  # type: ignore[index]
    await state.set_state(AddProduct.waiting_name)
    await message.answer("Фото принято! Теперь введите название товара:")


@router.callback_query(AddProduct.waiting_photo, F.data == "skip")
async def fsm_photo_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(photo_file_id=None)
    await state.set_state(AddProduct.waiting_name)
    await callback.message.answer("Введите название товара:")  # type: ignore[union-attr]


@router.message(AddProduct.waiting_name, F.text, ~F.text.startswith("/"))
async def fsm_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()[:255]
    if not name:
        await message.answer("Название не может быть пустым:")
        return
    await state.update_data(name=name)
    await state.set_state(AddProduct.waiting_description)
    await message.answer(
        "Введите описание товара или нажмите «Пропустить»:",
        reply_markup=skip_keyboard(),
    )


@router.message(AddProduct.waiting_description, F.text, ~F.text.startswith("/"))
async def fsm_description_message(message: Message, state: FSMContext) -> None:
    await state.update_data(description=(message.text or "").strip()[:4000] or None)
    await state.set_state(AddProduct.waiting_sku)
    await message.answer("Введите артикул (SKU) или нажмите «Пропустить»:", reply_markup=skip_keyboard())


@router.callback_query(AddProduct.waiting_description, F.data == "skip")
async def fsm_description_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(description=None)
    await state.set_state(AddProduct.waiting_sku)
    await callback.message.answer(  # type: ignore[union-attr]
        "Введите артикул (SKU) или нажмите «Пропустить»:", reply_markup=skip_keyboard()
    )


@router.message(AddProduct.waiting_sku, F.text, ~F.text.startswith("/"))
async def fsm_sku_message(message: Message, state: FSMContext) -> None:
    await state.update_data(sku=(message.text or "").strip()[:128] or None)
    await state.set_state(AddProduct.waiting_avito)
    await message.answer("Отправьте ссылку на объявление Avito или нажмите «Пропустить»:", reply_markup=skip_keyboard())


@router.callback_query(AddProduct.waiting_sku, F.data == "skip")
async def fsm_sku_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(sku=None)
    await state.set_state(AddProduct.waiting_avito)
    await callback.message.answer(  # type: ignore[union-attr]
        "Отправьте ссылку на объявление Avito или нажмите «Пропустить»:", reply_markup=skip_keyboard()
    )


@router.message(AddProduct.waiting_avito, F.text, ~F.text.startswith("/"))
async def fsm_avito_message(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if extract_avito_item_id(url) is None:
        await message.answer(
            "Не похоже на ссылку объявления Avito. Отправьте ссылку или нажмите «Пропустить»:",
            reply_markup=skip_keyboard(),
        )
        return
    await state.update_data(avito_url=url[:2000])
    await state.set_state(AddProduct.waiting_quantity)
    await message.answer("Введите начальное количество (целое число ≥ 0):")


@router.callback_query(AddProduct.waiting_avito, F.data == "skip")
async def fsm_avito_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(avito_url=None)
    await state.set_state(AddProduct.waiting_quantity)
    await callback.message.answer("Введите начальное количество (целое число ≥ 0):")  # type: ignore[union-attr]


@router.message(AddProduct.waiting_quantity, F.text, ~F.text.startswith("/"))
async def fsm_quantity(message: Message, state: FSMContext, session: AsyncSession) -> None:
    qty = parse_quantity(message.text)
    if qty is None:
        await message.answer("Пожалуйста, введите целое число ≥ 0:")
        return

    seller = await _get_seller(session, message.from_user.id)
    if seller is None:
        await state.clear()
        await message.answer(NOT_REGISTERED)
        return

    data = await state.get_data()
    product = await create_product(
        session,
        seller_id=seller.id,
        name=data.get("name", ""),
        description=data.get("description"),
        sku=data.get("sku"),
        quantity=qty,
        photo_file_id=data.get("photo_file_id"),
        avito_url=data.get("avito_url"),
    )
    await state.clear()
    logger.info("Product created id=%d (seller omitted)", product.id)
    await message.answer(f"✅ Товар «{escape(product.name)}» добавлен (кол-во: {qty}).")
    await _send_card(message, product)


@router.callback_query(F.data == "add_product")
async def cb_add_product(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer(NOT_REGISTERED)  # type: ignore[union-attr]
        return
    logger.info("Seller entered AddProduct FSM via inline button")
    await _start_add(callback.message, state)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Product card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("view:"))
async def cb_view_product(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer(NOT_REGISTERED)  # type: ignore[union-attr]
        return
    product = await get_product(session, _parse_id(callback.data), seller.id)
    if product is None:
        await callback.message.answer("Товар не найден.")  # type: ignore[union-attr]
        return
    await _send_card(callback.message, product)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("adj:"))
async def cb_adjust(callback: CallbackQuery, session: AsyncSession) -> None:
    """Quick ±1 from the card; updates the card in place."""
    product_id, delta = _parse_id(callback.data), _parse_id(callback.data, 2)
    seller = await _get_seller(session, callback.from_user.id)
    product = await get_product(session, product_id, seller.id) if seller else None
    if product is None:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    if product.quantity + delta < 0:
        await callback.answer("Остаток уже 0.")
        return
    product.quantity += delta
    await session.flush()
    await callback.answer(f"Остаток: {product.quantity}")

    text = format_product_card(product, with_description=True)
    keyboard = product_card_keyboard(product.id)
    msg = callback.message
    try:
        if product.photo_file_id and msg.photo:  # type: ignore[union-attr]
            await msg.edit_caption(caption=text[:1024], parse_mode="HTML", reply_markup=keyboard)  # type: ignore[union-attr]
        else:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore[union-attr]
    except TelegramBadRequest:
        await _send_card(msg, product)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Editing a single field
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("editmenu:"))
async def cb_edit_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(  # type: ignore[union-attr]
        "Что изменить?", reply_markup=edit_menu_keyboard(_parse_id(callback.data))
    )


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit_field(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    product_id = _parse_id(callback.data)
    field = (callback.data or "").split(":")[2]
    if field not in EDIT_FIELDS:
        return
    seller = await _get_seller(session, callback.from_user.id)
    product = await get_product(session, product_id, seller.id) if seller else None
    if product is None:
        await callback.message.answer("Товар не найден.")  # type: ignore[union-attr]
        return

    await state.set_state(EditProduct.waiting_value)
    await state.update_data(product_id=product_id, field=field)

    if field == "quantity":
        prompt = f"Сейчас: {product.quantity}. {QTY_PROMPT}"
    elif field == "photo":
        prompt = "Отправьте новое фото («-» — удалить фото):"
    elif field == "name":
        prompt = f"Сейчас: {escape(product.name)}\nВведите новое название:"
    else:
        current = getattr(product, field) or "—"
        prompt = f"Сейчас: {escape(str(current))}\nВведите новое значение («-» — очистить):"
    await callback.message.answer(prompt + "\n/cancel — отмена")  # type: ignore[union-attr]


async def _apply_edit(message: Message, state: FSMContext, session: AsyncSession, **fields: object) -> None:
    data = await state.get_data()
    seller = await _get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    product = (
        await update_product(session, data["product_id"], seller.id, **fields) if seller else None
    )
    await state.clear()
    if product is None:
        await message.answer("Товар не найден.")
        return
    await message.answer("✅ Сохранено.")
    await _send_card(message, product)


@router.message(EditProduct.waiting_value, F.photo)
async def fsm_edit_photo(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if (await state.get_data()).get("field") != "photo":
        await message.answer("Ожидаю текст. Введите значение или /cancel.")
        return
    await _apply_edit(message, state, session, photo_file_id=message.photo[-1].file_id)  # type: ignore[index]


@router.message(EditProduct.waiting_value, F.text, ~F.text.startswith("/"))
async def fsm_edit_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    field: str = data["field"]
    text = (message.text or "").strip()
    clear = text.lower() in CLEAR_MARKERS

    if field == "photo":
        if not clear:
            await message.answer("Отправьте фото или «-», чтобы удалить текущее.")
            return
        await _apply_edit(message, state, session, photo_file_id=None)
        return

    if field == "quantity":
        seller = await _get_seller(session, message.from_user.id)  # type: ignore[union-attr]
        product = await get_product(session, data["product_id"], seller.id) if seller else None
        qty = parse_quantity(text, product.quantity if product else None)
        if qty is None:
            await message.answer("Нужно число ≥ 0 или изменение вида +5 / -3 (итог не меньше 0):")
            return
        await _apply_edit(message, state, session, quantity=qty)
        return

    if field == "name":
        if clear or not text:
            await message.answer("Название не может быть пустым:")
            return
        await _apply_edit(message, state, session, name=text[:255])
        return

    if field == "avito_url" and not clear and extract_avito_item_id(text) is None:
        await message.answer("Не похоже на ссылку объявления Avito. Отправьте ссылку или «-»:")
        return

    limits = {"description": 4000, "sku": 128, "avito_url": 2000}
    await _apply_edit(message, state, session, **{field: None if clear else text[: limits[field]]})


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("delete:"))
async def cb_delete_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    product_id = _parse_id(callback.data)
    await callback.message.answer(  # type: ignore[union-attr]
        "Удалить товар безвозвратно?", reply_markup=confirm_delete_keyboard(product_id)
    )


@router.callback_query(F.data.startswith("delete_yes:"))
async def cb_delete_product(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    product_id = _parse_id(callback.data)
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer(NOT_REGISTERED)  # type: ignore[union-attr]
        return
    if await delete_product(session, product_id, seller.id):
        logger.info("Product deleted id=%d (seller omitted)", product_id)
        await callback.message.answer("🗑 Товар удалён.")  # type: ignore[union-attr]
    else:
        await callback.message.answer("Товар не найден.")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# List navigation
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "back_to_list")
async def cb_back_to_list(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer(NOT_REGISTERED)  # type: ignore[union-attr]
        return
    await _send_product_list(callback, session, seller.id, page=0)


@router.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer(NOT_REGISTERED)  # type: ignore[union-attr]
        return
    await _send_product_list(callback, session, seller.id, page=_parse_id(callback.data))


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM catch-all — MUST stay last: aiogram checks handlers in registration
# order, so anything above it gets the first chance at a matching message.
# ---------------------------------------------------------------------------


@router.message(StateFilter(AddProduct, EditProduct))
async def fsm_catch_all(message: Message) -> None:
    """Catch unexpected input (wrong content type, stray commands) inside FSM flows."""
    await message.answer("Ожидаю другое значение. Введите его или /cancel для отмены.")
