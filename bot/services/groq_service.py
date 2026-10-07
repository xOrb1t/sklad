import base64
import logging

from groq import AsyncGroq

from bot.config import settings

logger = logging.getLogger(__name__)


class GroqServiceError(Exception):
    pass


client: AsyncGroq | None = AsyncGroq(api_key=settings.GROQ_API_KEY) if settings.GROQ_API_KEY else None

_VISION_PROMPT = (
    "Определи товар на фото для поиска по складу. "
    "Ответь одной строкой: 3–8 ключевых слов по-русски через пробел — "
    "тип товара, бренд/модель (если видно), цвет, материал. Без пояснений."
)


def is_enabled() -> bool:
    return client is not None


def _client() -> AsyncGroq:
    if client is None:
        raise GroqServiceError("GROQ_API_KEY is not configured")
    return client


async def describe_image(image_bytes: bytes, seller_id: int = 0) -> str:
    """Call Groq Vision to get search keywords for a product photo (Russian)."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    try:
        response = await _client().chat.completions.create(
            model=settings.GROQ_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": _VISION_PROMPT},
                    ],
                }
            ],
            max_tokens=100,
            temperature=0.2,
        )
        result = (response.choices[0].message.content or "").strip()
    except GroqServiceError:
        raise
    except Exception as e:
        logger.warning("groq_describe_image error: %s", e)
        raise GroqServiceError(str(e)) from e
    logger.info("groq_describe_image seller=%d -> %d chars", seller_id, len(result))
    return result


async def transcribe_audio(audio_bytes: bytes, seller_id: int = 0) -> str:
    """Transcribe voice audio (OGG) to Russian text via Groq Whisper."""
    try:
        response = await _client().audio.transcriptions.create(
            model=settings.GROQ_WHISPER_MODEL,
            file=("voice.ogg", audio_bytes),
            language="ru",
        )
        result = response.text.strip()
    except GroqServiceError:
        raise
    except Exception as e:
        logger.warning("groq_transcribe_audio error: %s", e)
        raise GroqServiceError(str(e)) from e
    logger.info("groq_transcribe_audio seller=%d -> %d chars transcript", seller_id, len(result))
    return result
