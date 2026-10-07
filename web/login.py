"""Print a web-panel login link without Telegram (homelab / first setup).

    python -m web.login <telegram_id>
"""
from __future__ import annotations

import asyncio
import sys

from bot.database import async_session_factory, engine
from bot.services.seller_service import get_seller
from bot.services.web_auth import login_url


async def _main(telegram_id: int) -> int:
    async with async_session_factory() as session:
        seller = await get_seller(session, telegram_id)
    await engine.dispose()
    if seller is None:
        print(f"seller with telegram_id={telegram_id} not found — send /start to the bot first", file=sys.stderr)
        return 1
    print(login_url(seller.id))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_main(int(sys.argv[1]))))
