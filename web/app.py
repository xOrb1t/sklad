"""Web panel for Sklad: JSON API + static single-page UI.

Auth: the bot's /web command issues a short-lived signed login link
(bot/services/web_auth.py).  /auth trades it for a session cookie; each link
works once per web process.

Run:  uvicorn web.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal

from aiogram import Bot
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database import async_session_factory, engine
from bot.models import Product, Seller
from bot.services import groq_service, web_auth
from bot.services.product_service import (
    LOW_STOCK,
    create_product,
    delete_product,
    get_product,
    inventory_stats,
    list_products,
    update_product,
)
from bot.services.search_service import search_by_avito_url, search_by_sku, search_by_text
from bot.services.seller_service import get_seller_by_id

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
COOKIE = "sklad_session"
CSRF_HEADER = "x-sklad"

_used_login_tokens: dict[str, float] = {}
_photo_cache: OrderedDict[str, bytes] = OrderedDict()
_PHOTO_CACHE_MAX = 256
_bot: Bot | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    if _bot is not None:
        await _bot.session.close()
    await engine.dispose()


app = FastAPI(title="Sklad", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def db() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DB = Annotated[AsyncSession, Depends(db)]


async def current_seller(request: Request, session: DB) -> Seller:
    seller_id = web_auth.verify(request.cookies.get(COOKIE), web_auth.SESSION)
    seller = await get_seller_by_id(session, seller_id) if seller_id else None
    if seller is None:
        raise HTTPException(401, "unauthorized")
    # Cookie + SameSite=Lax is not enough for JSON writes; a custom header
    # forces a CORS preflight that cross-site pages cannot pass.
    if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "missing csrf header")
    return seller


CurrentSeller = Annotated[Seller, Depends(current_seller)]


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=settings.BOT_TOKEN)
    return _bot


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProductOut(BaseModel):
    id: int
    name: str
    description: str | None
    sku: str | None
    quantity: int
    avito_url: str | None
    has_photo: bool
    created_at: str

    @classmethod
    def of(cls, p: Product) -> "ProductOut":
        return cls(
            id=p.id,
            name=p.name,
            description=p.description,
            sku=p.sku,
            quantity=p.quantity,
            avito_url=p.avito_url,
            has_photo=bool(p.photo_file_id),
            created_at=p.created_at.isoformat() if p.created_at else "",
        )


def _blank_to_none(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    sku: str | None = Field(default=None, max_length=128)
    quantity: int = Field(default=0, ge=0, le=10_000_000)
    avito_url: str | None = Field(default=None, max_length=2000)

    _strip = field_validator("description", "sku", "avito_url")(_blank_to_none)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is empty")
        return v


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    sku: str | None = Field(default=None, max_length=128)
    quantity: int | None = Field(default=None, ge=0, le=10_000_000)
    avito_url: str | None = Field(default=None, max_length=2000)

    _strip = field_validator("description", "sku", "avito_url")(_blank_to_none)


class QtyDelta(BaseModel):
    delta: int = Field(ge=-1_000_000, le=1_000_000)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.get("/auth")
async def auth(t: str, request: Request, session: DB) -> Response:
    now = time.time()
    for tok, exp in list(_used_login_tokens.items()):
        if exp < now:
            del _used_login_tokens[tok]

    seller_id = web_auth.verify(t, web_auth.LOGIN, now)
    if seller_id is None or t in _used_login_tokens or await get_seller_by_id(session, seller_id) is None:
        return RedirectResponse("/?denied=1", status_code=303)
    _used_login_tokens[t] = now + settings.WEB_LOGIN_TTL

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE,
        web_auth.issue(seller_id, web_auth.SESSION, settings.WEB_SESSION_TTL),
        max_age=settings.WEB_SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=settings.WEB_BASE_URL.startswith("https://"),
    )
    return resp


@app.post("/api/logout", status_code=204)
async def logout(_: CurrentSeller) -> Response:
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
async def me(seller: CurrentSeller) -> dict[str, object]:
    return {"id": seller.id, "username": seller.username, "ai": groq_service.is_enabled()}


# ---------------------------------------------------------------------------
# Dashboard & search
# ---------------------------------------------------------------------------


@app.get("/api/stats")
async def stats(seller: CurrentSeller, session: DB) -> dict[str, object]:
    return {**await inventory_stats(session, seller.id), "low_stock": LOW_STOCK}


@app.get("/api/search")
async def search(
    seller: CurrentSeller,
    session: DB,
    q: Annotated[str, Query(min_length=1, max_length=500)],
) -> dict[str, object]:
    """Same cascade as the bot's /search: Avito URL → exact SKU → free text."""
    q = q.strip()
    if q.startswith("http"):
        product = await search_by_avito_url(session, seller.id, q)
        if product:
            return {"matched_by": "avito", "items": [ProductOut.of(product)]}
    product = await search_by_sku(session, seller.id, q)
    if product:
        return {"matched_by": "sku", "items": [ProductOut.of(product)]}
    items = await search_by_text(session, seller.id, q, limit=20)
    return {"matched_by": "text" if items else None, "items": [ProductOut.of(p) for p in items]}


# ---------------------------------------------------------------------------
# Products CRUD
# ---------------------------------------------------------------------------


@app.get("/api/products")
async def products(
    seller: CurrentSeller,
    session: DB,
    q: Annotated[str | None, Query(max_length=200)] = None,
    stock: Literal["depleted", "low"] | None = None,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    items, total = await list_products(
        session, seller.id, query=(q or "").strip() or None, stock=stock, offset=page * size, limit=size
    )
    return {"items": [ProductOut.of(p) for p in items], "total": total, "page": page, "size": size}


@app.post("/api/products", status_code=201)
async def add_product(body: ProductIn, seller: CurrentSeller, session: DB) -> ProductOut:
    p = await create_product(session, seller.id, **body.model_dump())
    await session.refresh(p)
    return ProductOut.of(p)


@app.patch("/api/products/{product_id}")
async def patch_product(product_id: int, body: ProductPatch, seller: CurrentSeller, session: DB) -> ProductOut:
    fields = body.model_dump(exclude_unset=True)
    if fields.get("name") is not None:
        fields["name"] = fields["name"].strip()
    if "name" in fields and not fields["name"]:
        raise HTTPException(422, "name is empty")
    if "quantity" in fields and fields["quantity"] is None:
        raise HTTPException(422, "quantity is required")
    p = await update_product(session, product_id, seller.id, **fields)
    if p is None:
        raise HTTPException(404, "not found")
    return ProductOut.of(p)


@app.post("/api/products/{product_id}/adjust")
async def adjust_qty(product_id: int, body: QtyDelta, seller: CurrentSeller, session: DB) -> ProductOut:
    p = await get_product(session, product_id, seller.id)
    if p is None:
        raise HTTPException(404, "not found")
    p.quantity = max(0, p.quantity + body.delta)
    await session.flush()
    return ProductOut.of(p)


@app.delete("/api/products/{product_id}", status_code=204)
async def remove_product(product_id: int, seller: CurrentSeller, session: DB) -> Response:
    if not await delete_product(session, product_id, seller.id):
        raise HTTPException(404, "not found")
    return Response(status_code=204)


@app.get("/api/products/{product_id}/photo")
async def product_photo(product_id: int, seller: CurrentSeller, session: DB) -> Response:
    """Proxy the Telegram file: file_id is bound to the bot token, browsers can't fetch it."""
    p = await get_product(session, product_id, seller.id)
    if p is None or not p.photo_file_id:
        raise HTTPException(404, "no photo")
    data = _photo_cache.get(p.photo_file_id)
    if data is None:
        buf = BytesIO()
        try:
            await get_bot().download(p.photo_file_id, destination=buf)
        except Exception as e:  # noqa: BLE001
            logger.warning("photo download failed product=%d: %s", product_id, e)
            raise HTTPException(502, "telegram unavailable") from e
        data = buf.getvalue()
        _photo_cache[p.photo_file_id] = data
        while len(_photo_cache) > _PHOTO_CACHE_MAX:
            _photo_cache.popitem(last=False)
    else:
        _photo_cache.move_to_end(p.photo_file_id)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
