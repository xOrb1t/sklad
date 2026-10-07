"""Tests for the AI search handlers and groq_service wrappers.

Groq client is always mocked — no real API calls in CI.
Handler-level tests mock describe_image / transcribe_audio / search_by_text
directly to verify control-flow logic (empty result, error path, happy path).
"""
from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Seller
from bot.services.groq_service import (
    GroqServiceError,
    describe_image,
    extract_item,
    transcribe_audio,
)
from bot.services.query_terms import ExtractedItem
from bot.services.product_service import create_product


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Yield a fresh in-memory SQLite session with the full schema.

    Registers a Unicode-aware ``lower()`` function with every new SQLite
    connection so that Cyrillic (and other non-ASCII) text search works
    correctly in tests.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _register_unicode_lower(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
        dbapi_conn.create_function("lower", 1, lambda s: s.lower() if s else s)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session

    await engine.dispose()


async def _make_seller(session: AsyncSession, telegram_id: int) -> Seller:
    seller = Seller(telegram_id=telegram_id)
    session.add(seller)
    await session.flush()
    return seller


def _make_message(telegram_id: int = 42) -> MagicMock:
    """Build a minimal mock Message that records answer() calls."""
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = telegram_id
    msg.answer = AsyncMock()
    return msg


def _make_bot() -> MagicMock:
    """Build a minimal mock Bot."""
    bot = MagicMock()
    bot.download = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# groq_service unit tests (mock Groq client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_image_calls_groq() -> None:
    """describe_image returns the content string from the Groq chat response."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "красные кроссовки"

    with patch("bot.services.groq_service.client") as mock_client:
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await describe_image(b"fake")

    assert result == "красные кроссовки"


@pytest.mark.asyncio
async def test_transcribe_audio_calls_groq() -> None:
    """transcribe_audio returns the .text field from the Groq transcription response."""
    mock_response = MagicMock()
    mock_response.text = "есть ли красные кроссовки"

    with patch("bot.services.groq_service.client") as mock_client:
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)
        result = await transcribe_audio(b"fake_audio")

    assert result == "есть ли красные кроссовки"


@pytest.mark.asyncio
async def test_describe_image_groq_error() -> None:
    """describe_image raises GroqServiceError when the Groq call fails."""
    with patch("bot.services.groq_service.client") as mock_client:
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))
        with pytest.raises(GroqServiceError):
            await describe_image(b"x")


@pytest.mark.asyncio
async def test_extract_item_parses_json() -> None:
    """extract_item parses the JSON answer (even wrapped in a code fence)."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = (
        '```json\n{"category": "sneakers", "brand": "Nike", "model": "Dunk Low", '
        '"style_code": null, "colors": ["white", "black"], "confidence": 0.8}\n```'
    )
    with patch("bot.services.groq_service.client") as mock_client:
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        item = await extract_item(b"fake", caption="панда")

    assert item.brand == "Nike"
    assert item.model == "Dunk Low"
    assert item.colors == ["white", "black"]
    sent_text = mock_client.chat.completions.create.call_args.kwargs["messages"][0]["content"][1]["text"]
    assert "панда" in sent_text


@pytest.mark.asyncio
async def test_extract_item_bad_json_raises() -> None:
    """Unparseable model output surfaces as GroqServiceError."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "Это белые кроссовки"
    with patch("bot.services.groq_service.client") as mock_client:
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        with pytest.raises(GroqServiceError):
            await extract_item(b"fake")


@pytest.mark.asyncio
async def test_transcribe_audio_groq_error() -> None:
    """transcribe_audio raises GroqServiceError when the Groq call fails."""
    with patch("bot.services.groq_service.client") as mock_client:
        mock_client.audio.transcriptions.create = AsyncMock(side_effect=Exception("API error"))
        with pytest.raises(GroqServiceError):
            await transcribe_audio(b"x")


# ---------------------------------------------------------------------------
# Handler integration tests (real DB, mocked Groq + search)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_photo_search_returns_product(db_session: AsyncSession) -> None:
    """Photo handler searches by extracted attributes (not the raw description) and replies."""
    from bot.handlers.ai_search import handle_photo_search

    seller = await _make_seller(db_session, telegram_id=100)
    await create_product(db_session, seller.id, name="Красные кроссовки Puma")
    await create_product(db_session, seller.id, name="Синие джинсы Levi's")

    msg = _make_message(telegram_id=100)
    photo_size = MagicMock()
    photo_size.file_id = "file123"
    msg.photo = [photo_size]
    msg.caption = None
    bot = _make_bot()

    item = ExtractedItem(category="sneakers", brand="Puma", colors=["red"], title_ru="Красные кроссовки")
    with patch("bot.handlers.ai_search.extract_item", AsyncMock(return_value=item)):
        await handle_photo_search(msg, bot, db_session)

    # "Обрабатываю..." + results
    assert msg.answer.call_count == 2
    reply = msg.answer.call_args_list[-1][0][0]
    assert "Красные кроссовки Puma" in reply
    assert "джинсы" not in reply
    assert "Распознано" in reply


@pytest.mark.asyncio
async def test_photo_search_style_code_exact(db_session: AsyncSession) -> None:
    """A style code read from the tag wins over fuzzy matching."""
    from bot.handlers.ai_search import handle_photo_search

    seller = await _make_seller(db_session, telegram_id=105)
    await create_product(db_session, seller.id, name="Nike Air Force 1 белые")
    await create_product(db_session, seller.id, name="Nike AF1 белые оригинал", sku="CW2288-111")

    msg = _make_message(telegram_id=105)
    msg.photo = [MagicMock()]
    msg.caption = None
    bot = _make_bot()

    item = ExtractedItem(brand="Nike", model="Air Force 1 07", style_code="cw2288-111", colors=["white"])
    with patch("bot.handlers.ai_search.extract_item", AsyncMock(return_value=item)):
        await handle_photo_search(msg, bot, db_session)

    reply = msg.answer.call_args_list[-1][0][0]
    assert "оригинал" in reply
    assert reply.count("📦") == 1


@pytest.mark.asyncio
async def test_photo_search_empty_description(db_session: AsyncSession) -> None:
    """Photo handler reports unrecognised photo and does NOT search when extraction is empty."""
    from bot.handlers.ai_search import handle_photo_search

    seller = await _make_seller(db_session, telegram_id=101)

    msg = _make_message(telegram_id=101)
    msg.photo = [MagicMock()]
    msg.caption = None
    bot = _make_bot()

    mock_search = AsyncMock()

    with (
        patch("bot.handlers.ai_search.extract_item", AsyncMock(return_value=ExtractedItem())),
        patch("bot.handlers.ai_search.search_by_terms", mock_search),
    ):
        await handle_photo_search(msg, bot, db_session)

    mock_search.assert_not_called()
    last_call_text = msg.answer.call_args_list[-1][0][0]
    assert "Не удалось распознать" in last_call_text


@pytest.mark.asyncio
async def test_voice_search_returns_product(db_session: AsyncSession) -> None:
    """Voice handler calls search_by_text with the transcript and replies with results."""
    from bot.handlers.ai_search import handle_voice_search

    seller = await _make_seller(db_session, telegram_id=102)
    product = await create_product(db_session, seller.id, name="Синие ботинки")

    msg = _make_message(telegram_id=102)
    voice_obj = MagicMock()
    voice_obj.file_id = "voice456"
    msg.voice = voice_obj
    bot = _make_bot()

    mock_search = AsyncMock(return_value=[product])

    with (
        patch("bot.handlers.ai_search.transcribe_audio", AsyncMock(return_value="синие ботинки")),
        patch("bot.handlers.ai_search.search_by_text", mock_search),
    ):
        await handle_voice_search(msg, bot, db_session)

    mock_search.assert_called_once_with(db_session, seller.id, "синие ботинки", limit=3)
    assert msg.answer.call_count == 2


@pytest.mark.asyncio
async def test_photo_search_groq_error_returns_graceful_message(db_session: AsyncSession) -> None:
    """Photo handler replies with the degradation message when GroqServiceError is raised."""
    from bot.handlers.ai_search import handle_photo_search

    seller = await _make_seller(db_session, telegram_id=103)

    msg = _make_message(telegram_id=103)
    photo_size = MagicMock()
    msg.photo = [photo_size]
    bot = _make_bot()

    msg.caption = None
    with patch("bot.handlers.ai_search.extract_item", AsyncMock(side_effect=GroqServiceError("fail"))):
        await handle_photo_search(msg, bot, db_session)

    replies = [call[0][0] for call in msg.answer.call_args_list]
    assert any("Сервис временно недоступен" in r for r in replies)


@pytest.mark.asyncio
async def test_photo_search_unregistered_seller(db_session: AsyncSession) -> None:
    """Photo handler replies '/start' prompt when seller is not registered."""
    from bot.handlers.ai_search import handle_photo_search

    msg = _make_message(telegram_id=999)
    photo_size = MagicMock()
    msg.photo = [photo_size]
    bot = _make_bot()

    with patch("bot.handlers.ai_search.extract_item", AsyncMock()) as mock_desc:
        await handle_photo_search(msg, bot, db_session)

    mock_desc.assert_not_called()
    last_reply = msg.answer.call_args_list[-1][0][0]
    assert "/start" in last_reply


@pytest.mark.asyncio
async def test_voice_search_empty_transcript(db_session: AsyncSession) -> None:
    """Voice handler replies 'Ничего не найдено.' and does NOT call search_by_text when transcript is empty."""
    from bot.handlers.ai_search import handle_voice_search

    seller = await _make_seller(db_session, telegram_id=104)

    msg = _make_message(telegram_id=104)
    voice_obj = MagicMock()
    msg.voice = voice_obj
    bot = _make_bot()

    mock_search = AsyncMock()

    with (
        patch("bot.handlers.ai_search.transcribe_audio", AsyncMock(return_value="")),
        patch("bot.handlers.ai_search.search_by_text", mock_search),
    ):
        await handle_voice_search(msg, bot, db_session)

    mock_search.assert_not_called()
    last_call_text = msg.answer.call_args_list[-1][0][0]
    assert "Ничего не найдено" in last_call_text
