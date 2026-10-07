"""
bot/startup.py — Docker entry point.

Runs Alembic migrations programmatically (overriding the hardcoded localhost URL
in alembic.ini with the runtime DATABASE_URL from settings), then starts the bot.

Usage (Docker CMD / direct):
    python -m bot.startup

Migrations run *before* the event loop starts: alembic/env.py calls
``asyncio.run()`` itself, which fails inside an already running loop.
"""

import asyncio
import logging

from alembic import command
from alembic.config import Config

from bot.config import settings

logger = logging.getLogger(__name__)


def run_migrations() -> None:
    """Apply all pending Alembic migrations before starting the bot.

    Creates a fresh Config object pointing at alembic.ini, overrides
    ``sqlalchemy.url`` with the runtime DATABASE_URL so the hardcoded
    ``localhost/sklad`` value in alembic.ini is never used at runtime,
    then delegates to ``alembic.command.upgrade`` which drives env.py
    through alembic's internal script runner.
    """
    logger.info("Running Alembic migrations…")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    command.upgrade(cfg, "head")
    logger.info("Migrations complete.")


def start() -> None:
    """Run migrations then start the bot."""
    run_migrations()
    # Import main lazily: it configures logging (after alembic's fileConfig)
    from main import main  # noqa: PLC0415

    asyncio.run(main())


if __name__ == "__main__":
    start()
