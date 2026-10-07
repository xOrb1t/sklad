"""Stock movements, low-stock alerts, CSV import/export, migrations."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Product, Seller, StockMovement
from bot.services.alerts import build_alert
from bot.services.csv_service import CsvImportError, export_csv, import_csv
from bot.services.product_service import (
    StockChange,
    adjust_quantity,
    create_product,
    delete_product,
    get_history,
    inventory_stats,
    list_products,
    update_product,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _lower(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.create_function("lower", 1, lambda s: s.lower() if s else s)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seller(session: AsyncSession, tg: int = 1, threshold: int = 2) -> Seller:
    s = Seller(telegram_id=tg, low_stock_threshold=threshold, notify_low_stock=True)
    session.add(s)
    await session.flush()
    return s


# --------------------------------------------------------------------- movements


async def test_movements_are_logged(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    p = await create_product(db_session, s.id, "A", quantity=5)
    await adjust_quantity(db_session, p, -2, "web")
    assert await adjust_quantity(db_session, p, 0, "web") is None  # no-op is not logged
    await update_product(db_session, p.id, s.id, quantity=10, source="import")
    await adjust_quantity(db_session, p, -50, "bot")  # clamped at 0

    hist = await get_history(db_session, p.id, s.id)
    assert [(m.delta, m.quantity_after, m.source) for m in hist] == [
        (-10, 0, "bot"), (7, 10, "import"), (-2, 3, "web"), (5, 5, "bot"),
    ]
    assert await get_history(db_session, p.id, s.id + 1) == []  # tenant-safe

    assert await delete_product(db_session, p.id, s.id)
    left = (await db_session.execute(select(func.count(StockMovement.id)))).scalar_one()
    assert left == 0


async def test_zero_initial_quantity_not_logged(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    p = await create_product(db_session, s.id, "A")
    assert await get_history(db_session, p.id, s.id) == []


async def test_per_seller_threshold(db_session: AsyncSession) -> None:
    s = await _seller(db_session, threshold=5)
    await create_product(db_session, s.id, "A", quantity=4)
    await create_product(db_session, s.id, "B", quantity=6)
    st = await inventory_stats(db_session, s.id, low_stock=5)
    assert st["low"] == 1
    items, total = await list_products(db_session, s.id, stock="low", low_stock=5)
    assert total == 1 and items[0].name == "A"


# --------------------------------------------------------------------- alerts


@pytest.mark.parametrize(
    "old,new,expected",
    [(5, 2, "low"), (5, 0, "out"), (2, 0, "out"), (2, 1, None), (0, 1, None), (1, 3, None), (5, 4, None)],
)
def test_alert_crossing(old: int, new: int, expected: str | None) -> None:
    assert StockChange(Product(name="x"), old, new).alert(2) == expected


def test_build_alert_respects_setting_and_escapes() -> None:
    seller = Seller(telegram_id=1, notify_low_stock=True, low_stock_threshold=2)
    changes = [StockChange(Product(name="<A>"), 5, 0), StockChange(Product(name="B"), 9, 2), StockChange(Product(name="C"), 1, 3)]
    text = build_alert(seller, changes)
    assert text and "&lt;A&gt;" in text and "закончился" in text and "осталось 2" in text and "C" not in text
    seller.notify_low_stock = False
    assert build_alert(seller, changes) is None


# --------------------------------------------------------------------- csv


async def test_csv_roundtrip_is_noop(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    await create_product(db_session, s.id, "Фильтр; масляный", sku="A1", quantity=3, description='с "кавычками"')
    await create_product(db_session, s.id, "Без артикула", quantity=1)
    items, _ = await list_products(db_session, s.id)
    res = await import_csv(db_session, s.id, export_csv(items))
    assert (res.created, res.updated, res.unchanged) == (0, 0, 2)


async def test_csv_import_create_update(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    p = await create_product(db_session, s.id, "Старое имя", sku="SKU-1", quantity=10, description="keep")
    data = (
        "Название,Артикул,Количество,Ссылка\n"
        "Новое имя,SKU-1,1,\n"  # match by sku; empty link keeps nothing
        "Свечи,NGK,4,https://www.avito.ru/msk/zapchasti/svechi_123456789\n"
        "\n"
    ).encode("cp1251")
    res = await import_csv(db_session, s.id, data)
    assert (res.created, res.updated) == (1, 1)
    assert p.name == "Новое имя" and p.quantity == 1 and p.description == "keep"
    assert [c.alert(2) for c in res.changes] == ["low"]
    items, _ = await list_products(db_session, s.id, query="свечи")
    assert items[0].avito_item_id == "123456789" and items[0].quantity == 4
    assert [m.source for m in await get_history(db_session, items[0].id, s.id)] == ["import"]


async def test_csv_import_is_all_or_nothing(db_session: AsyncSession) -> None:
    s = await _seller(db_session)
    data = (
        "name;sku;quantity;avito_url;id\n"
        "Ok;X1;1;;\n"
        ";;2;;\n"  # no name for new product
        "Bad qty;X2;-1;;\n"
        "Dup;X1;1;;\n"
        "Link;X3;1;https://example.com/;\n"
        "Ghost;;1;;9999\n"
    ).encode()
    with pytest.raises(CsvImportError) as ei:
        await import_csv(db_session, s.id, data)
    errs = ei.value.errors
    assert any(e.startswith("строка 3") for e in errs)
    assert any("строка 4" in e and "количество" in e for e in errs)
    assert any("строка 5" in e and "повторяется" in e for e in errs)
    assert any("строка 6" in e and "Avito" in e for e in errs)
    assert any("строка 7" in e and "9999" in e for e in errs)
    assert (await list_products(db_session, s.id))[1] == 0


async def test_csv_import_other_sellers_id_rejected(db_session: AsyncSession) -> None:
    a, b = await _seller(db_session, 1), await _seller(db_session, 2)
    foreign = await create_product(db_session, b.id, "чужой", quantity=5)
    with pytest.raises(CsvImportError):
        await import_csv(db_session, a.id, f"id;quantity\n{foreign.id};0\n".encode())
    assert foreign.quantity == 5


@pytest.mark.parametrize("data", [b"", b"foo;bar\n1;2\n", b"name\n"])
async def test_csv_import_bad_files(db_session: AsyncSession, data: bytes) -> None:
    s = await _seller(db_session)
    with pytest.raises(CsvImportError):
        await import_csv(db_session, s.id, data)


# --------------------------------------------------------------------- migrations


def test_migrations_upgrade_downgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alembic import command
    from alembic.config import Config

    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    command.upgrade(cfg, "head")
