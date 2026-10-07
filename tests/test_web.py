"""Web panel API: auth flow, CSRF guard, tenant isolation, CRUD."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.models import Base, Seller
from bot.services import web_auth
from web import app as web_app

H = {"X-Sklad": "1"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            s.add_all([Seller(telegram_id=1, username="a"), Seller(telegram_id=2, username="b")])
            await s.commit()

    monkeypatch.setattr(web_app, "async_session_factory", factory)
    monkeypatch.setattr(web_app, "engine", engine)
    web_app._used_login_tokens.clear()
    with TestClient(web_app.app) as c:
        c.portal.call(setup)
        yield c


def login(c: TestClient, seller_id: int) -> None:
    tok = web_auth.issue(seller_id, web_auth.LOGIN, 60)
    r = c.get(f"/auth?t={tok}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401
    assert client.get("/").status_code == 200


def test_login_link_is_single_use(client: TestClient) -> None:
    tok = web_auth.issue(1, web_auth.LOGIN, 60)
    assert client.get(f"/auth?t={tok}", follow_redirects=False).headers["location"] == "/"
    client.cookies.clear()
    assert "denied" in client.get(f"/auth?t={tok}", follow_redirects=False).headers["location"]
    session_tok = web_auth.issue(1, web_auth.SESSION, 60)
    assert "denied" in client.get(f"/auth?t={session_tok}", follow_redirects=False).headers["location"]


def test_crud_flow(client: TestClient) -> None:
    login(client, 1)
    assert client.get("/api/me").json()["username"] == "a"

    body = {"name": " Фильтр ", "sku": "", "quantity": 1,
            "avito_url": "https://www.avito.ru/moskva/zapchasti/filtr_1234567890"}
    assert client.post("/api/products", json=body).status_code == 403  # no CSRF header
    r = client.post("/api/products", json=body, headers=H)
    assert r.status_code == 201
    p = r.json()
    assert p["name"] == "Фильтр" and p["sku"] is None

    assert client.post(f"/api/products/{p['id']}/adjust", json={"delta": -5}, headers=H).json()["quantity"] == 0
    assert client.get("/api/stats").json()["depleted"] == 1

    r = client.patch(f"/api/products/{p['id']}", json={"quantity": 3, "description": "B46"}, headers=H)
    assert r.json()["quantity"] == 3 and r.json()["description"] == "B46"
    assert client.patch(f"/api/products/{p['id']}", json={"quantity": -1}, headers=H).status_code == 422
    assert client.patch(f"/api/products/{p['id']}", json={"quantity": None}, headers=H).status_code == 422
    assert client.patch(f"/api/products/{p['id']}", json={"name": None}, headers=H).status_code == 422

    found = client.get("/api/search", params={"q": body["avito_url"] + "?x=1"}).json()
    assert found["matched_by"] == "avito" and found["items"][0]["id"] == p["id"]
    assert client.get("/api/products", params={"q": "b46"}).json()["total"] == 1

    assert client.delete(f"/api/products/{p['id']}", headers=H).status_code == 204
    assert client.get("/api/products").json()["total"] == 0


def test_tenant_isolation(client: TestClient) -> None:
    login(client, 1)
    pid = client.post("/api/products", json={"name": "mine"}, headers=H).json()["id"]
    client.cookies.clear()
    login(client, 2)
    assert client.get("/api/products").json()["total"] == 0
    assert client.patch(f"/api/products/{pid}", json={"name": "x"}, headers=H).status_code == 404
    assert client.delete(f"/api/products/{pid}", headers=H).status_code == 404
    assert client.get(f"/api/products/{pid}/photo").status_code == 404


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.deleted: list[int] = []

    async def send_photo(self, chat_id: int, photo, disable_notification: bool = False):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        self.sent.append(chat_id)
        return SimpleNamespace(message_id=99, photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="BIG")])

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append(message_id)
        return True


def test_photo_upload_and_delete(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBot()
    monkeypatch.setattr(web_app, "get_bot", lambda: fake)
    login(client, 1)
    pid = client.post("/api/products", json={"name": "p"}, headers=H).json()["id"]

    url = f"/api/products/{pid}/photo"
    assert client.put(url, content=b"x", headers={**H, "Content-Type": "text/plain"}).status_code == 415
    assert client.put(url, content=b"", headers={**H, "Content-Type": "image/jpeg"}).status_code == 422
    big = b"0" * (web_app.MAX_PHOTO_BYTES + 1)
    assert client.put(url, content=big, headers={**H, "Content-Type": "image/jpeg"}).status_code == 413

    r = client.put(url, content=b"\xff\xd8jpeg", headers={**H, "Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.json()["has_photo"] and r.json()["photo_key"]
    assert fake.sent == [1] and fake.deleted == [99]  # sent to the seller's own chat, then cleaned up

    r = client.delete(url, headers=H)
    assert r.status_code == 200 and not r.json()["has_photo"] and r.json()["photo_key"] is None


def test_export_csv(client: TestClient) -> None:
    login(client, 1)
    client.post("/api/products", json={"name": "Фильтр; масляный", "sku": "A1", "quantity": 2}, headers=H)
    r = client.get("/api/export.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿id;name;")
    assert '"Фильтр; масляный";;A1;2;' in text


def test_history_settings_and_alerts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(web_app, "_send_text", fake_send)
    login(client, 1)
    pid = client.post("/api/products", json={"name": "Свечи", "quantity": 5}, headers=H).json()["id"]

    client.post(f"/api/products/{pid}/adjust", json={"delta": -3}, headers=H)  # 5 → 2: low
    assert len(sent) == 1 and sent[0][0] == 1 and "осталось 2" in sent[0][1]
    client.post(f"/api/products/{pid}/adjust", json={"delta": -1}, headers=H)  # already low: no repeat
    assert len(sent) == 1
    client.patch(f"/api/products/{pid}", json={"quantity": 0}, headers=H)  # → out
    assert len(sent) == 2 and "закончился" in sent[1][1]

    hist = client.get(f"/api/products/{pid}/history").json()
    assert [(m["delta"], m["quantity_after"], m["source"]) for m in hist] == [
        (-1, 0, "web"), (-1, 1, "web"), (-3, 2, "web"), (5, 5, "web"),
    ]

    assert client.patch("/api/settings", json={"low_stock_threshold": -1}, headers=H).status_code == 422
    r = client.patch("/api/settings", json={"notify_low_stock": False, "low_stock_threshold": 10}, headers=H)
    assert r.json() == {"notify_low_stock": False, "low_stock_threshold": 10}
    assert client.get("/api/me").json()["settings"]["low_stock_threshold"] == 10
    client.post(f"/api/products/{pid}/adjust", json={"delta": 20}, headers=H)
    client.post(f"/api/products/{pid}/adjust", json={"delta": -15}, headers=H)  # 20 → 5 ≤ 10, but muted
    assert len(sent) == 2
    stats = client.get("/api/stats").json()
    assert stats["low_stock"] == 10 and stats["low"] == 1


def test_import_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "_send_text", lambda *a: None)
    login(client, 1)
    ok = "name;sku;quantity\nФильтр;F1;3\nСвечи;S1;0\n".encode()
    r = client.post("/api/import.csv", content=ok, headers={**H, "Content-Type": "text/csv"})
    assert r.json() == {"created": 2, "updated": 0, "unchanged": 0}

    bad = b"name;sku;quantity\nX;F9;abc\n"
    r = client.post("/api/import.csv", content=bad, headers={**H, "Content-Type": "text/csv"})
    assert r.status_code == 422 and r.json()["detail"]["total"] == 1
    assert client.get("/api/products").json()["total"] == 2

    exported = client.get("/api/export.csv").content
    r = client.post("/api/import.csv", content=exported, headers={**H, "Content-Type": "text/csv"})
    assert r.json() == {"created": 0, "updated": 0, "unchanged": 2}
