"""Turn free text or a vision extraction into weighted search term groups.

A *term group* is a set of alternative substrings (e.g. ``{"nike", "найк"}``);
a product matches the group if any alternative occurs in its name,
description or SKU. Groups carry a weight so that a brand or style-code hit
outranks a colour hit. This is a stop-gap until the hybrid FTS/vector search
from ``docs/context-base.md`` lands — it works on the current schema and on
SQLite in tests.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError, field_validator


# ---------------------------------------------------------------------------
# Dictionaries
# ---------------------------------------------------------------------------

# canonical brand -> alternatives (lower-case, ё→е). Latin + Cyrillic + slang.
BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "nike": ("nike", "найк"),
    "jordan": ("jordan", "джордан", "жордан"),
    "adidas": ("adidas", "адидас", "адик"),
    "new balance": ("new balance", "newbalance", "нью беланс", "нью баланс", "нб "),
    "puma": ("puma", "пума"),
    "reebok": ("reebok", "рибок"),
    "asics": ("asics", "асикс"),
    "converse": ("converse", "конверс"),
    "vans": ("vans", "ванс"),
    "salomon": ("salomon", "саломон"),
    "on": ("on running", "cloud"),
    "hoka": ("hoka", "хока"),
    "yeezy": ("yeezy", "изи", "ебоост"),
    "the north face": ("north face", "tnf", "норт фейс", "тнф"),
    "stone island": ("stone island", "стон айленд", "стоун айленд", "стоник"),
    "carhartt": ("carhartt", "кархарт"),
    "stussy": ("stussy", "стусси", "стасси"),
    "supreme": ("supreme", "суприм"),
    "levi's": ("levi", "левайс", "левис"),
    "lacoste": ("lacoste", "лакост"),
    "ralph lauren": ("ralph lauren", "polo ralph", "ральф лорен"),
    "tommy hilfiger": ("tommy", "томми"),
    "calvin klein": ("calvin klein", "кельвин кляйн", "кляйн"),
    "balenciaga": ("balenciaga", "баленсиага"),
    "gucci": ("gucci", "гучи", "гуччи"),
    "dr. martens": ("martens", "мартинс", "мартенс"),
    "timberland": ("timberland", "тимберленд", "тимбы"),
    "ugg": ("ugg", "угг", "угги"),
    "crocs": ("crocs", "крокс"),
    "fila": ("fila", "фила"),
    "kappa": ("kappa", "каппа"),
    "under armour": ("under armour", "андер армор"),
    "mizuno": ("mizuno", "мизуно"),
    "saucony": ("saucony", "сакони"),
    "onitsuka tiger": ("onitsuka", "оницука"),
}

# Slang model names → alternatives. Matched against normalised text.
MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "air force": ("air force", "af1", "форс"),
    "air max": ("air max", "аир макс", "эйр макс"),
    "dunk": ("dunk", "данк"),
    "jordan 1": ("jordan 1", "aj1", "джордан 1"),
    "samba": ("samba", "самба"),
    "gazelle": ("gazelle", "газел"),
    "superstar": ("superstar", "суперстар"),
    "campus": ("campus", "кампус"),
    "yeezy": ("yeezy", "изи"),
    "550": ("550",),
    "574": ("574",),
    "9060": ("9060",),
    "gel-kayano": ("kayano", "каяно"),
    "xt-6": ("xt-6", "xt6"),
}

# canonical colour (as the VLM is instructed to return) → alternatives
COLOR_ALIASES: dict[str, tuple[str, ...]] = {
    "black": ("black", "черн"),
    "white": ("white", "бел"),
    "grey": ("grey", "gray", "сер"),
    "beige": ("beige", "беж"),
    "cream": ("cream", "кремов", "молочн"),
    "brown": ("brown", "коричн"),
    "red": ("red", "красн"),
    "burgundy": ("burgundy", "бордо", "бордов"),
    "pink": ("pink", "розов"),
    "orange": ("orange", "оранж"),
    "yellow": ("yellow", "желт"),
    "green": ("green", "зелен"),
    "olive": ("olive", "оливк"),
    "khaki": ("khaki", "хаки"),
    "blue": ("blue", "син"),
    "navy": ("navy", "темно-син", "темносин"),
    "lightblue": ("light blue", "голуб"),
    "purple": ("purple", "фиолет"),
}

CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "sneakers": ("кроссов", "кед", "sneaker"),
    "shoes": ("туфл", "лофер", "мокасин", "shoe"),
    "boots": ("ботин", "сапог", "boot", "тимбер", "челси"),
    "slides": ("шлеп", "сланц", "слайд", "сандал", "slide"),
    "tshirt": ("футбол", "tee", "t-shirt"),
    "longsleeve": ("лонгслив", "longsleeve"),
    "hoodie": ("худи", "hoodie", "толстовк"),
    "sweatshirt": ("свитшот", "sweatshirt", "свитер", "джемпер"),
    "jacket": ("куртк", "пуховик", "ветровк", "бомбер", "jacket", "анорак"),
    "coat": ("пальто", "coat", "парк"),
    "pants": ("штаны", "брюк", "джоггер", "карго", "pants"),
    "shorts": ("шорт", "shorts"),
    "jeans": ("джинс", "jeans"),
    "cap": ("кепк", "бейсболк", "шапк", "панам", "cap"),
    "bag": ("сумк", "рюкзак", "шоппер", "bag"),
    "accessory": ("ремень", "носк", "перчатк", "шарф"),
}

# Words that carry no product signal in a search phrase.
STOPWORDS = frozenset(
    "есть ли а и в во на с со по для из у к от до не нет да это эти этот та те то "
    "мне нужно нужны нужен нужна надо найди найти покажи показать подбери какие какой "
    "какая что где пожалуйста плиз размер размера размеры р шт штук пару пара "
    "the a an of for with".split()
)

# Weights per group kind — tuned so that style code > brand/model > category > colour.
W_STYLE_CODE = 6
W_BRAND = 3
W_MODEL = 3
W_CATEGORY = 2
W_COLOR = 1
W_WORD = 1

_RU_ENDING = re.compile(
    r"(иями|ями|ами|ого|его|ому|ему|ыми|ими|ией|иях|ие|ые|ое|ее|ий|ый|ой|ая|яя|ую|юю|"
    r"ов|ев|ей|ам|ям|ах|ях|ом|ем|ки|ка|ку|ок|и|ы|а|я|е|о|у|ю)$"
)
_TOKEN = re.compile(r"[a-zа-я0-9]+(?:-[a-zа-я0-9]+)*")
_STYLE_CODE = re.compile(r"\b([A-Z]{1,3}\d{3,5}-\d{3}|[A-Z]{2}\d{4}|[A-Z]\d{5})\b")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TermGroup:
    alternatives: tuple[str, ...]
    weight: int = W_WORD
    kind: str = "word"


@dataclass
class SearchQuery:
    groups: list[TermGroup] = field(default_factory=list)
    style_code: str | None = None

    def add(self, group: TermGroup) -> None:
        if group.alternatives and group not in self.groups:
            self.groups.append(group)


class ExtractedItem(BaseModel):
    """Structured product attributes returned by the vision model."""

    category: str | None = None
    brand: str | None = None
    model: str | None = None
    colorway: str | None = None
    style_code: str | None = None
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    title_ru: str | None = None
    confidence: float = 0.0

    @field_validator("colors", "materials", "visible_text", mode="before")
    @classmethod
    def _none_to_list(cls, v):  # type: ignore[no-untyped-def]
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    def is_empty(self) -> bool:
        return not any(
            (self.category, self.brand, self.model, self.style_code, self.colors, self.title_ru)
        )

    def summary(self) -> str:
        parts = [
            " ".join(x for x in (self.brand, self.model) if x),
            self.colorway or ", ".join(self.colors),
            self.style_code or "",
        ]
        return " · ".join(p for p in parts if p) or (self.title_ru or "")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def stem(word: str) -> str:
    """Very rough Russian suffix stripping; keeps at least 4 chars."""
    if not re.search(r"[а-я]", word) or len(word) <= 4:
        return word
    stripped = _RU_ENDING.sub("", word)
    return stripped if len(stripped) >= 4 else word[:4]


def parse_extraction(raw: str) -> ExtractedItem:
    """Parse the VLM's JSON answer (tolerates ```json fences and prose around it)."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model output")
    try:
        return ExtractedItem.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"invalid extraction JSON: {e}") from e


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _dict_groups(
    text: str, table: dict[str, tuple[str, ...]], weight: int, kind: str
) -> list[tuple[TermGroup, list[str]]]:
    """Return groups whose alias occurs in *text* plus the alias strings that hit."""
    hits = []
    padded = f" {text} "
    for canon, alts in table.items():
        found = [a for a in alts if a in padded]
        if found:
            hits.append((TermGroup((canon, *alts), weight, kind), found))
    return hits


def query_from_text(text: str) -> SearchQuery:
    """Build term groups from a free-text / voice query."""
    q = SearchQuery()
    code = _STYLE_CODE.search(text.upper())
    if code:
        q.style_code = code.group(1)
        q.add(TermGroup((code.group(1).lower(),), W_STYLE_CODE, "style_code"))

    norm = normalize(text)
    consumed: set[str] = set()
    for table, weight, kind in (
        (BRAND_ALIASES, W_BRAND, "brand"),
        (MODEL_ALIASES, W_MODEL, "model"),
        (CATEGORY_ALIASES, W_CATEGORY, "category"),
        (COLOR_ALIASES, W_COLOR, "color"),
    ):
        for group, found in _dict_groups(norm, table, weight, kind):
            q.add(group)
            for alias in found:
                consumed.update(_TOKEN.findall(alias))

    for token in _TOKEN.findall(norm):
        if token in STOPWORDS or token.isdigit() or len(token) < 3:
            continue
        if any(token.startswith(c) or c.startswith(token) for c in consumed):
            continue
        q.add(TermGroup((stem(token),), W_WORD, "word"))
    return q


def query_from_item(item: ExtractedItem, caption: str | None = None) -> SearchQuery:
    """Build term groups from a vision extraction (optionally merged with a photo caption)."""
    q = SearchQuery()
    if item.style_code:
        code = item.style_code.strip().upper()
        q.style_code = code
        q.add(TermGroup((code.lower(),), W_STYLE_CODE, "style_code"))

    if item.brand:
        b = normalize(item.brand)
        alts = next(
            (v for k, v in BRAND_ALIASES.items() if k == b or any(a.strip() in b for a in v)),
            (),
        )
        q.add(TermGroup((b, *alts), W_BRAND, "brand"))

    if item.model:
        m = normalize(item.model)
        alts = next((v for k, v in MODEL_ALIASES.items() if k in m), ())
        q.add(TermGroup((m, *alts), W_MODEL, "model"))
        # Individual distinctive model tokens ("force", "samba", "550") as weak hits.
        for token in _TOKEN.findall(m):
            if len(token) >= 3 and token not in STOPWORDS and token != m:
                q.add(TermGroup((token,), W_WORD, "word"))

    if item.category:
        cat = normalize(item.category)
        if cat in CATEGORY_ALIASES:
            q.add(TermGroup(CATEGORY_ALIASES[cat], W_CATEGORY, "category"))

    for color in item.colors[:2]:
        c = normalize(color).replace(" ", "")
        if c in COLOR_ALIASES:
            q.add(TermGroup(COLOR_ALIASES[c], W_COLOR, "color"))

    if caption:
        extra = query_from_text(caption)
        for group in extra.groups:
            q.add(group)
        q.style_code = q.style_code or extra.style_code
    return q
