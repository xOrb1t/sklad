import base64
import logging

from groq import AsyncGroq

from bot.config import settings

logger = logging.getLogger(__name__)


class GroqServiceError(Exception):
    pass


client = AsyncGroq(api_key=settings.GROQ_API_KEY)


async def describe_image(image_bytes: bytes) -> str:
    """Call Groq Vision to describe a product image in Russian."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    try:
        response = await client.chat.completions.create(
            model="llama-3.2-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                        {
                            "type": "text",
                            "text": (
                                "Опиши, что за товар изображён на фото. "
                                "Укажи название, цвет, материал, особенности. "
                                "Отвечай по-русски."
                            ),
                        },
                    ],
                }
            ],
            max_tokens=200,
        )
        result = response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("groq_describe_image error: %s", e)
        raise GroqServiceError(str(e)) from e
    logger.info("groq_describe_image -> %d chars", len(result))
    return result


async def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe voice audio (OGG) to Russian text via Groq Whisper."""
    try:
        response = await client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=("voice.ogg", audio_bytes),
            language="ru",
        )
        result = response.text.strip()
    except Exception as e:
        logger.warning("groq_transcribe_audio error: %s", e)
        raise GroqServiceError(str(e)) from e
    logger.info("groq_transcribe_audio -> %d chars", len(result))
    return result
