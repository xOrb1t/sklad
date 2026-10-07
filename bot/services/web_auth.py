"""Stateless HMAC tokens for the web panel.

Format: ``<purpose>.<seller_id>.<exp>.<sig>`` where ``sig`` is a truncated
HMAC-SHA256 over the first three fields.  ``purpose`` separates short-lived
login links (sent by the bot) from long-lived session cookies, so a leaked
cookie cannot be replayed as a link and vice versa.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from bot.config import settings

LOGIN = "l"
SESSION = "s"


def _key() -> bytes:
    if settings.WEB_SECRET:
        return settings.WEB_SECRET.encode()
    return hashlib.sha256(b"sklad-web:" + settings.BOT_TOKEN.encode()).digest()


def _sign(payload: str) -> str:
    mac = hmac.new(_key(), payload.encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def issue(seller_id: int, purpose: str, ttl: int, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl)
    payload = f"{purpose}.{seller_id}.{exp}"
    return f"{payload}.{_sign(payload)}"


def verify(token: str | None, purpose: str, now: float | None = None) -> int | None:
    """Return seller_id for a valid, unexpired token of *purpose*, else None."""
    if not token:
        return None
    try:
        p, sid, exp, sig = token.split(".")
        seller_id, expires = int(sid), int(exp)
    except ValueError:
        return None
    if p != purpose or not hmac.compare_digest(sig, _sign(f"{p}.{sid}.{exp}")):
        return None
    if expires < (now if now is not None else time.time()):
        return None
    return seller_id


def login_url(seller_id: int) -> str:
    token = issue(seller_id, LOGIN, settings.WEB_LOGIN_TTL)
    return f"{settings.WEB_BASE_URL.rstrip('/')}/auth?t={token}"
