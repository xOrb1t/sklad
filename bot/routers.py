from aiogram import Dispatcher

from bot.handlers.start import router as start_router


def register_routers(dp: Dispatcher) -> None:
    """Include all feature routers into the dispatcher."""
    dp.include_router(start_router)
