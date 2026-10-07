"""Inline keyboard builders for product CRUD flows."""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup

if TYPE_CHECKING:
    from bot.models import Product

PAGE_SIZE = 10  # must match handler default


def product_list_keyboard(
    products: list["Product"],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Keyboard for /myproducts list: one button per product + pagination + add."""
    builder = InlineKeyboardBuilder()

    for product in products:
        builder.button(
            text=f"📦 {product.name} (кол-во: {product.quantity})",
            callback_data=f"view:{product.id}",
        )

    # Pagination row — shown only when there is more than one page
    if total_pages > 1:
        pagination_buttons: list[tuple[str, str]] = []
        if page > 0:
            pagination_buttons.append(("◀️ Назад", f"page:{page - 1}"))
        pagination_buttons.append((f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            pagination_buttons.append(("Вперёд ▶️", f"page:{page + 1}"))

        for text, data in pagination_buttons:
            builder.button(text=text, callback_data=data)

        # Keep pagination buttons on one row
        if len(products) > 0:
            builder.adjust(*([1] * len(products)), len(pagination_buttons))
        else:
            builder.adjust(len(pagination_buttons))
    else:
        builder.adjust(1)

    builder.button(text="➕ Добавить товар", callback_data="add_product")

    return builder.as_markup()


def product_card_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Keyboard for a single product card: update qty, delete, back."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить количество", callback_data=f"qty:{product_id}")
    builder.button(text="🗑 Удалить", callback_data=f"delete:{product_id}")
    builder.button(text="◀️ Назад к списку", callback_data="back_to_list")
    builder.adjust(1)
    return builder.as_markup()


def skip_keyboard() -> InlineKeyboardMarkup:
    """Single 'skip' button — used in optional FSM steps."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Пропустить", callback_data="skip")
    return builder.as_markup()


def confirm_delete_keyboard(product_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить", callback_data=f"delete_yes:{product_id}")
    builder.button(text="✖️ Отмена", callback_data=f"view:{product_id}")
    builder.adjust(2)
    return builder.as_markup()


def web_login_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🖥 Открыть панель", url=url)
    return builder.as_markup()
