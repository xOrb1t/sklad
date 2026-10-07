"""HTML formatting for product cards (parse_mode=HTML — user input must be escaped)."""
from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.models import Product


def format_product_card(product: "Product", *, with_description: bool = False) -> str:
    lines = [f"📦 <b>{escape(product.name)}</b>", f"Кол-во: {product.quantity}"]
    if with_description and product.description:
        lines.append(f"Описание: {escape(product.description)}")
    if product.sku:
        lines.append(f"Артикул: {escape(product.sku)}")
    if product.avito_url:
        lines.append(f"Avito: {escape(product.avito_url)}")
    return "\n".join(lines)
