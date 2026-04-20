from aiogram import Dispatcher

from bot.handlers.products import router as products_router
from bot.handlers.start import router as start_router


def register_routers(dp: Dispatcher) -> None:
    """Include all feature routers into the dispatcher."""
    dp.include_router(start_router)
    dp.include_router(products_router)
