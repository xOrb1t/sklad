"""CSV export/import of products.

Import is all-or-nothing: every row is validated first, and nothing is
written when any row has an error.  Rows are matched to existing products by
``id`` (round-trip of an export), then by ``sku``; otherwise a product is
created.  Empty cells leave existing values untouched.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Product
from bot.services.product_service import StockChange, create_product, set_quantity
from bot.utils.avito_url import extract_avito_item_id

EXPORT_FIELDS = ("id", "name", "description", "sku", "quantity", "avito_url", "created_at")
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

# header aliases → canonical column
_ALIASES = {
    "id": "id", "#": "id",
    "name": "name", "название": "name", "наименование": "name", "товар": "name",
    "description": "description", "описание": "description",
    "sku": "sku", "артикул": "sku",
    "quantity": "quantity", "количество": "quantity", "кол-во": "quantity", "остаток": "quantity",
    "avito_url": "avito_url", "avito": "avito_url", "ссылка": "avito_url", "ссылка avito": "avito_url",
}
_LIMITS = {"name": 255, "description": 4000, "sku": 128, "avito_url": 2000}


class CsvImportError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    changes: list[StockChange] = field(default_factory=list)

    def summary(self) -> str:
        return f"создано: {self.created}, обновлено: {self.updated}, без изменений: {self.unchanged}"


def export_csv(products: list[Product]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(EXPORT_FIELDS)
    for p in products:
        w.writerow([
            p.id, p.name, p.description or "", p.sku or "", p.quantity, p.avito_url or "",
            p.created_at.isoformat() if p.created_at else "",
        ])
    # BOM so Excel detects UTF-8 (Cyrillic)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvImportError(["не удалось определить кодировку (нужен UTF-8 или Windows-1251)"])


@dataclass
class _Row:
    line: int
    values: dict[str, str]


def parse_csv(data: bytes) -> list[_Row]:
    if len(data) > MAX_BYTES:
        raise CsvImportError([f"файл больше {MAX_BYTES // 1024 // 1024} МБ"])
    text = _decode(data)
    first = text.split("\n", 1)[0]
    delimiter = max((";", ",", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise CsvImportError(["файл пустой"]) from None

    columns = [_ALIASES.get(h.strip().lower()) for h in header]
    if "name" not in columns and "sku" not in columns and "id" not in columns:
        raise CsvImportError(["нет колонки name/название (или sku/id для обновления)"])

    rows: list[_Row] = []
    for line, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        values = {col: cell.strip() for col, cell in zip(columns, cells) if col}
        rows.append(_Row(line, values))
        if len(rows) > MAX_ROWS:
            raise CsvImportError([f"больше {MAX_ROWS} строк"])
    if not rows:
        raise CsvImportError(["нет строк с данными"])
    return rows


async def import_csv(session: AsyncSession, seller_id: int, data: bytes) -> ImportResult:
    rows = parse_csv(data)
    existing = list((await session.execute(select(Product).where(Product.seller_id == seller_id))).scalars())
    by_id = {p.id: p for p in existing}
    by_sku = {p.sku: p for p in sorted(existing, key=lambda p: p.id) if p.sku}

    errors: list[str] = []
    plan: list[tuple[Product | None, dict[str, object]]] = []
    seen: set[int] = set()
    seen_new_skus: set[str] = set()

    for row in rows:
        v = row.values
        err = lambda msg: errors.append(f"строка {row.line}: {msg}")  # noqa: E731

        target: Product | None = None
        if v.get("id"):
            if not v["id"].isdigit() or int(v["id"]) not in by_id:
                err(f"товар с id {v['id']} не найден")
                continue
            target = by_id[int(v["id"])]
        elif v.get("sku") and v["sku"] in by_sku:
            target = by_sku[v["sku"]]

        if target is not None:
            if target.id in seen:
                err("товар встречается в файле дважды")
                continue
            seen.add(target.id)
        else:
            if not v.get("name"):
                err("для нового товара нужно название")
                continue
            if v.get("sku"):
                if v["sku"] in seen_new_skus:
                    err(f"артикул {v['sku']} повторяется")
                    continue
                seen_new_skus.add(v["sku"])

        fields: dict[str, object] = {}
        for col, limit in _LIMITS.items():
            if v.get(col):
                if len(v[col]) > limit:
                    err(f"{col} длиннее {limit} символов")
                fields[col] = v[col]
        if v.get("avito_url") and extract_avito_item_id(v["avito_url"]) is None:
            err("ссылка Avito не распознана")
        if v.get("quantity"):
            q = v["quantity"].replace(" ", "")
            if not q.isdigit():
                err(f"количество «{v['quantity']}» — нужно целое ≥ 0")
            else:
                fields["quantity"] = int(q)
        plan.append((target, fields))

    if errors:
        raise CsvImportError(errors)

    result = ImportResult()
    for target, fields in plan:
        qty = fields.pop("quantity", None)
        if target is None:
            p = await create_product(
                session, seller_id, str(fields.pop("name")), quantity=0, source="import", **fields  # type: ignore[arg-type]
            )
            if qty:
                await set_quantity(session, p, int(qty), "import")  # type: ignore[call-overload]
            result.created += 1
            continue
        dirty = False
        for key, value in fields.items():
            if getattr(target, key) != value:
                setattr(target, key, value)
                dirty = True
        if "avito_url" in fields:
            target.avito_item_id = extract_avito_item_id(target.avito_url)
        if qty is not None:
            change = await set_quantity(session, target, int(qty), "import")  # type: ignore[call-overload]
            if change:
                result.changes.append(change)
                dirty = True
        if dirty:
            result.updated += 1
        else:
            result.unchanged += 1
    await session.flush()
    return result
