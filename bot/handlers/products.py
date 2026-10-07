"""Product CRUD Telegram handlers: /myproducts, /addproduct FSM, inline callbacks."""
from __future__ import annotations

import logging
import math
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.formatting import format_product_card
from bot.keyboards import (
    confirm_delete_keyboard,
    product_card_keyboard,
    product_list_keyboard,
    skip_keyboard,
)
from bot.services.product_service import (
    count_products,
    create_product,
    delete_product,
    get_product,
    get_products,
    update_quantity,
)
from bot.services.seller_service import get_seller as _get_seller
from bot.states import AddProduct, UpdatingQuantity

logger = logging.getLogger(__name__)

router = Router(name="products")

PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# Internal helper
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
        except Exception:
            await target.message.answer(text, reply_markup=keyboard)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@router.message(Command("myproducts"))
async def cmd_myproducts(message: Message, session: AsyncSession) -> None:
    seller = await _get_seller(session, message.from_user.id)
    if seller is None:
        await message.answer("Сначала нажмите /start")
        return
    await _send_product_list(message, session, seller.id, page=0)


@router.message(Command("addproduct"))
async def cmd_addproduct(message: Message, session: AsyncSession, state: FSMContext) -> None:
    seller = await _get_seller(session, message.from_user.id)
    if seller is None:
        await message.answer("Сначала нажмите /start")
        return

    await state.set_state(AddProduct.waiting_photo)
    logger.info("Seller entered AddProduct FSM")
    await message.answer(
        "Отправьте фото товара или нажмите «Пропустить»:",
        reply_markup=skip_keyboard(),
    )


# ---------------------------------------------------------------------------
# FSM: /cancel inside AddProduct (catch-all lives at the end of the module)
# ---------------------------------------------------------------------------


@router.message(StateFilter(AddProduct, UpdatingQuantity), Command("cancel"))
async def cmd_cancel_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    logger.info("Seller exited FSM via /cancel")
    await message.answer("Отменено.")


# ---------------------------------------------------------------------------
# FSM steps: AddProduct
# ---------------------------------------------------------------------------


@router.message(AddProduct.waiting_photo, F.photo)
async def fsm_photo_message(message: Message, state: FSMContext) -> None:
    file_id = message.photo[-1].file_id
    await state.update_data(photo_file_id=file_id)
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
    await state.update_data(description=message.text)
    await state.set_state(AddProduct.waiting_sku)
    await message.answer(
        "Введите артикул (SKU) или нажмите «Пропустить»:",
        reply_markup=skip_keyboard(),
    )


@router.callback_query(AddProduct.waiting_description, F.data == "skip")
async def fsm_description_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(description=None)
    await state.set_state(AddProduct.waiting_sku)
    await callback.message.answer(  # type: ignore[union-attr]
        "Введите артикул (SKU) или нажмите «Пропустить»:",
        reply_markup=skip_keyboard(),
    )


@router.message(AddProduct.waiting_sku, F.text, ~F.text.startswith("/"))
async def fsm_sku_message(message: Message, state: FSMContext) -> None:
    await state.update_data(sku=(message.text or "").strip()[:128] or None)
    await state.set_state(AddProduct.waiting_quantity)
    await message.answer("Введите начальное количество (целое число ≥ 0):")


@router.callback_query(AddProduct.waiting_sku, F.data == "skip")
async def fsm_sku_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(sku=None)
    await state.set_state(AddProduct.waiting_quantity)
    await callback.message.answer("Введите начальное количество (целое число ≥ 0):")  # type: ignore[union-attr]


@router.message(AddProduct.waiting_quantity, F.text)
async def fsm_quantity(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    text = (message.text or "").strip()
    try:
        qty = int(text)
        if qty < 0:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите целое число ≥ 0:")
        return

    # Retrieve seller
    seller = await _get_seller(session, message.from_user.id)
    if seller is None:
        await state.clear()
        await message.answer("Сначала нажмите /start")
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
    )
    await state.clear()
    logger.info("Product created id=%d (seller omitted)", product.id)
    await message.answer(f"✅ Товар «{escape(product.name)}» успешно добавлен (кол-во: {qty}).")


# ---------------------------------------------------------------------------
# Callback: "add_product" button inside list
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "add_product")
async def cb_add_product(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer("Сначала нажмите /start")  # type: ignore[union-attr]
        return
    await state.set_state(AddProduct.waiting_photo)
    logger.info("Seller entered AddProduct FSM via inline button")
    await callback.message.answer(  # type: ignore[union-attr]
        "Отправьте фото товара или нажмите «Пропустить»:",
        reply_markup=skip_keyboard(),
    )


# ---------------------------------------------------------------------------
# Callback: view product card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("view:"))
async def cb_view_product(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    product_id = int(callback.data.split(":")[1])  # type: ignore[index]

    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer("Сначала нажмите /start")  # type: ignore[union-attr]
        return

    product = await get_product(session, product_id, seller.id)
    if product is None:
        await callback.message.answer("Товар не найден.")  # type: ignore[union-attr]
        return

    text = format_product_card(product, with_description=True)
    keyboard = product_card_keyboard(product.id)
    if product.photo_file_id:
        await callback.message.answer_photo(  # type: ignore[union-attr]
            product.photo_file_id, caption=text[:1024], parse_mode="HTML", reply_markup=keyboard
        )
    else:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Callback: update quantity (prompt + FSM)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("qty:"))
async def cb_qty_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    product_id = int(callback.data.split(":")[1])  # type: ignore[index]
    await state.set_state(UpdatingQuantity.waiting_qty)
    await state.update_data(product_id=product_id)
    await callback.message.answer("Введите новое количество (целое число ≥ 0):")  # type: ignore[union-attr]


@router.message(UpdatingQuantity.waiting_qty, F.text)
async def fsm_update_qty(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    text = (message.text or "").strip()
    try:
        qty = int(text)
        if qty < 0:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите целое число ≥ 0:")
        return

    seller = await _get_seller(session, message.from_user.id)
    if seller is None:
        await state.clear()
        await message.answer("Сначала нажмите /start")
        return

    data = await state.get_data()
    product_id: int = data["product_id"]

    updated = await update_quantity(session, product_id, seller.id, quantity=qty)
    await state.clear()

    if updated:
        await message.answer(f"✅ Количество обновлено: {qty}.")
    else:
        await message.answer("Товар не найден.")


# ---------------------------------------------------------------------------
# Callback: delete product
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("delete:"))
async def cb_delete_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    product_id = int(callback.data.split(":")[1])  # type: ignore[index]
    await callback.message.answer(  # type: ignore[union-attr]
        "Удалить товар безвозвратно?", reply_markup=confirm_delete_keyboard(product_id)
    )


@router.callback_query(F.data.startswith("delete_yes:"))
async def cb_delete_product(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    product_id = int(callback.data.split(":")[1])  # type: ignore[index]

    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer("Сначала нажмите /start")  # type: ignore[union-attr]
        return

    deleted = await delete_product(session, product_id, seller.id)
    if deleted:
        logger.info("Product deleted id=%d (seller omitted)", product_id)
        await callback.message.answer("🗑 Товар удалён.")  # type: ignore[union-attr]
    else:
        await callback.message.answer("Товар не найден.")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Callback: back to list
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "back_to_list")
async def cb_back_to_list(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer("Сначала нажмите /start")  # type: ignore[union-attr]
        return
    await _send_product_list(callback, session, seller.id, page=0)


# ---------------------------------------------------------------------------
# Callbacks: pagination
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    page = int(callback.data.split(":")[1])  # type: ignore[index]
    seller = await _get_seller(session, callback.from_user.id)
    if seller is None:
        await callback.message.answer("Сначала нажмите /start")  # type: ignore[union-attr]
        return
    await _send_product_list(callback, session, seller.id, page=page)


# ---------------------------------------------------------------------------
# Callback: noop (page indicator button)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM catch-all — MUST stay last: aiogram checks handlers in registration
# order, so anything above it gets the first chance at a matching message.
# ---------------------------------------------------------------------------


@router.message(StateFilter(AddProduct, UpdatingQuantity))
async def fsm_catch_all(message: Message) -> None:
    """Catch unexpected input (wrong content type, stray commands) inside FSM flows."""
    await message.answer("Ожидаю другое значение. Введите его или /cancel для отмены.")
