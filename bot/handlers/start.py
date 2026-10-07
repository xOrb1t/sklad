import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.keyboards import web_login_keyboard
from bot.models import Seller
from bot.services.seller_service import get_seller
from bot.services.web_auth import login_url

logger = logging.getLogger(__name__)

router = Router(name="start")

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/myproducts — список товаров\n"
    "/addproduct — добавить товар\n"
    "/search &lt;артикул|ссылка Avito|текст&gt; — поиск\n"
    "/web — ссылка на веб-панель\n"
    "/cancel — отменить текущий ввод\n\n"
    "Просто текст — поиск по названию/описанию/артикулу.\n"
    "Фото или голосовое — AI-поиск."
)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext | None = None) -> None:
    """Register a new seller on /start, or greet a returning one (idempotent)."""
    if state is not None:
        await state.clear()
    seller = await get_seller(session, message.from_user.id)

    if seller is None:
        seller = Seller(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
        )
        session.add(seller)
        await session.flush()  # obtain PK before middleware commit
        logger.info("New seller registered (telegram_id omitted for privacy)")
        await message.answer("Добро пожаловать! Вы зарегистрированы как продавец.\n\n" + HELP_TEXT)
    else:
        await message.answer("С возвращением!")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("web"))
async def cmd_web(message: Message, session: AsyncSession) -> None:
    seller = await get_seller(session, message.from_user.id)  # type: ignore[union-attr]
    if seller is None:
        await message.answer("Сначала нажмите /start")
        return
    url = login_url(seller.id)
    minutes = settings.WEB_LOGIN_TTL // 60
    text = f"Одноразовая ссылка в панель, действует {minutes} мин. Никому её не пересылайте."
    try:
        await message.answer(text, reply_markup=web_login_keyboard(url))
    except TelegramBadRequest:
        # Telegram rejects localhost/LAN URLs in buttons — fall back to plain text
        await message.answer(f"{text}\n\n{url}", disable_web_page_preview=True)
