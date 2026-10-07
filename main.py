import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.config import settings
from bot.database import async_session_factory, engine
from bot.middlewares import DbSessionMiddleware
from bot.routers import register_routers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    force=True,  # override alembic's fileConfig when started via bot/startup.py
)
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Attach DB session middleware to every incoming update
    dp.update.middleware(DbSessionMiddleware(async_session_factory))

    # Register all feature routers
    register_routers(dp)

    try:
        await bot.set_my_commands([
            BotCommand(command="myproducts", description="Мои товары"),
            BotCommand(command="addproduct", description="Добавить товар"),
            BotCommand(command="search", description="Поиск"),
            BotCommand(command="web", description="Веб-панель"),
            BotCommand(command="alerts", description="Уведомления об остатках"),
            BotCommand(command="export", description="Выгрузка в CSV"),
            BotCommand(command="cancel", description="Отменить ввод"),
            BotCommand(command="help", description="Справка"),
        ])
        logger.info("Starting polling…")
        await dp.start_polling(bot)
    finally:
        await engine.dispose()
        await bot.session.close()
        logger.info("Bot stopped, engine disposed.")


if __name__ == "__main__":
    asyncio.run(main())
