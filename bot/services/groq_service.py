import base64
import logging

from groq import AsyncGroq

from bot.config import settings
from bot.services.query_terms import ExtractedItem, parse_extraction

logger = logging.getLogger(__name__)


class GroqServiceError(Exception):
    pass


client = AsyncGroq(api_key=settings.GROQ_API_KEY)


async def describe_image(image_bytes: bytes, seller_id: int = 0) -> str:
    """Call Groq Vision to describe a product image in Russian."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    try:
        response = await client.chat.completions.create(
            model=settings.VISION_MODEL,
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
    logger.info("groq_describe_image seller=%d -> %d chars", seller_id, len(result))
    return result


async def transcribe_audio(audio_bytes: bytes, seller_id: int = 0) -> str:
    """Transcribe voice audio (OGG) to Russian text via Groq Whisper."""
    try:
        response = await client.audio.transcriptions.create(
            model=settings.WHISPER_MODEL,
            file=("voice.ogg", audio_bytes),
            language="ru",
        )
        result = response.text.strip()
    except Exception as e:
        logger.warning("groq_transcribe_audio error: %s", e)
        raise GroqServiceError(str(e)) from e
    logger.info("groq_transcribe_audio seller=%d -> %d chars transcript", seller_id, len(result))
    return result


EXTRACT_PROMPT = """Ты каталогизатор магазина одежды и кроссовок. По фото товара верни ТОЛЬКО JSON-объект:
{
  "category": одно из [sneakers, shoes, boots, slides, tshirt, longsleeve, hoodie, sweatshirt,
               jacket, coat, pants, shorts, jeans, cap, bag, accessory, other],
  "brand": "Nike" | null,
  "model": "модель без бренда, напр. Air Force 1 07, Samba OG, 550" | null,
  "colorway": "название расцветки, если видно" | null,
  "style_code": "артикул производителя с бирки/коробки, напр. CW2288-111" | null,
  "colors": до 3 основных цветов из [black, white, grey, beige, cream, brown, red, burgundy, pink,
             orange, yellow, green, olive, khaki, blue, navy, lightblue, purple],
  "materials": ["suede", "leather", ...],
  "visible_text": ["весь читаемый текст на товаре/бирке"],
  "title_ru": "короткое название по-русски",
  "confidence": 0.0-1.0
}
Правила: style_code переписывай символ в символ, только если он реально виден — не угадывай.
Не уверен в бренде или модели — ставь null. Без пояснений, только JSON."""


async def extract_item(
    image_bytes: bytes, caption: str | None = None, seller_id: int = 0
) -> ExtractedItem:
    """Call the vision model and return structured product attributes."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    text = EXTRACT_PROMPT
    if caption:
        text += f"\n\nКомментарий пользователя к фото (приоритетнее фото): {caption}"
    try:
        response = await client.chat.completions.create(
            model=settings.VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": text},
                    ],
                }
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=400,
        )
        item = parse_extraction(response.choices[0].message.content or "")
    except Exception as e:
        logger.warning("groq_extract_item error: %s", e)
        raise GroqServiceError(str(e)) from e
    logger.info("groq_extract_item seller=%d -> %s", seller_id, item.model_dump(exclude_defaults=True))
    return item
