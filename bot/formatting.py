"""HTML formatting for product cards (parse_mode=HTML — user input must be escaped)."""
from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.models import Product


def format_product_card(
    product: "Product", *, with_description: bool = False, low_stock: int | None = None
) -> str:
    qty = f"Кол-во: {product.quantity}"
    if low_stock is not None:
        if product.quantity <= 0:
            qty += "  ❌ нет в наличии"
        elif product.quantity <= low_stock:
            qty += "  ⚠️ заканчивается"
    lines = [f"📦 <b>{escape(product.name)}</b>", qty]
    if with_description and product.description:
        lines.append(f"Описание: {escape(product.description)}")
    if product.sku:
        lines.append(f"Артикул: {escape(product.sku)}")
    if product.avito_url:
        lines.append(f"Avito: {escape(product.avito_url)}")
    return "\n".join(lines)


SOURCE_LABELS = {"bot": "бот", "web": "веб", "import": "импорт"}


def format_history(product: "Product", movements: list) -> str:  # type: ignore[type-arg]
    if not movements:
        return f"📜 <b>{escape(product.name)}</b>\nИстория пуста."
    lines = [f"📜 <b>{escape(product.name)}</b> — последние изменения:"]
    for m in movements:
        when = m.created_at.strftime("%d.%m %H:%M") if m.created_at else "—"
        lines.append(f"<code>{when}</code>  {m.delta:+d} → {m.quantity_after}  ({SOURCE_LABELS.get(m.source, m.source)})")
    return "\n".join(lines)
