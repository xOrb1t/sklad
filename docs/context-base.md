# Контекстная база позиций и поиск по фото/описанию

Дизайн-документ. Цель — по фото (своему или присланному клиентом), голосу или
свободному тексту («чёрные джорданы 42», «худи стоник серое L») за 1–3 секунды
находить конкретную позицию на складе с остатками по размерам. Интерфейс —
Telegram (бот + inline-режим, позже Mini App).

---

## 1. Что не так сейчас

| Проблема | Где | Последствие |
|---|---|---|
| Поиск по фото ищет весь ответ VLM через `LIKE '%<абзац текста>%'` | `bot/handlers/ai_search.py` → `search_by_text` | совпадений практически не бывает |
| `LIKE` без морфологии, без транслита и синонимов | `search_service.search_by_text` | «найки» ≠ «Nike», «кроссовок» ≠ «кроссовки» |
| Одна позиция = одна строка с `quantity` | `models.Product` | нет размерной сетки, а для обуви и одежды это главное |
| Хранится только `photo_file_id` | `models.Product` | `file_id` привязан к токену бота (D005), байтов нет, пересчитать эмбеддинги нельзя |
| Одно фото на товар | `models.Product` | нет ракурсов: подошва, бирка, этикетка коробки |
| `llama-3.2-vision-preview` | `groq_service.py` | Groq эту модель снял, нужен настраиваемый `VISION_MODEL` |

Вывод: искать по сырому тексту от VLM нельзя. Нужны: (а) структурированная карточка
позиции, (б) визуальные эмбеддинги фото, (в) гибридный текстовый поиск,
(г) объединение результатов через RRF.

---

## 2. Архитектура

```
             ┌───────────── Telegram (бот / inline / Mini App) ────────────┐
             │ фото · голос · текст                                        │
             └───────────────┬─────────────────────────────────────────────┘
                             │ aiogram
                 ┌───────────▼───────────┐
                 │  bot (ingest/search)  │──── Groq / OpenRouter / Ollama
                 └───┬───────────┬───────┘     (VLM-извлечение, Whisper, парсер запроса)
                     │           │ HTTP
                     │   ┌───────▼────────┐
                     │   │ embedder       │  SigLIP2 (фото+текст), bge-m3 (текст)
                     │   │ FastAPI, CPU   │
                     │   └────────────────┘
          ┌──────────▼──────────┐   ┌──────────────────┐
          │ Postgres 16         │   │ /data/photos     │  оригиналы, ключ sha256
          │ pgvector + pg_trgm  │   │ (volume)         │
          └─────────────────────┘   └──────────────────┘
```

Всё self-hosted, кроме LLM-вызовов. LLM вызываются через интерфейс, бэкенды
взаимозаменяемы: Groq, OpenRouter или Ollama (`qwen2.5vl:7b`).
Groq и часть провайдеров OpenRouter блокируют RU-IP, поэтому трафик бота к ним
идёт через WG-туннель к VPS. Подходят и policy-routing, и `HTTPS_PROXY`.

### Почему именно так

| Компонент | Выбор | Альтернативы | Почему |
|---|---|---|---|
| Векторное хранилище | **pgvector** (HNSW) в существующем Postgres | Qdrant, Milvus | каталог до ~100k фото, нужны фильтры по `seller_id`, размеру и остаткам в одном SQL; отдельный сервис — лишний хоп и синхронизация |
| Фото-эмбеддинги | **SigLIP2** `google/siglip2-base-patch16-256` (768d, Apache-2.0) | OpenCLIP ViT-B/32, jina-clip-v2 | многоязычный текстовый энкодер (запрос «красные кроссовки» → фото), коммерческая лицензия; у jina-clip-v2 лицензия CC-BY-NC |
| Текстовые эмбеддинги | **bge-m3** (1024d, MIT), через Ollama или sentence-transformers | multilingual-e5-large | хорошо работает с RU/EN-смесью («найк air force белые») |
| Лексический поиск | `tsvector` (`russian` + `simple`) + `pg_trgm` | Meilisearch | опечатки и транслит через trgm + словарь алиасов, без отдельного сервиса |
| Слияние | **RRF** (k=60) | взвешенная сумма скоров | скоры разных природ несравнимы; RRF не требует калибровки |
| Очередь фоновых задач | таблица + `FOR UPDATE SKIP LOCKED` | arq/Redis, Celery | без Redis; задач мало (эмбеддинг при добавлении) |

---

## 3. Модель данных

Позиция («модель», например Nike Air Force 1 '07 White, `CW2288-111`) и её
**варианты** (размер → остаток) разделены. Ищем по позиции, фильтруем и
показываем остатки по вариантам.

```sql
-- alembic/versions/0002_context_base.py (op.execute)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE brands (
    id       serial PRIMARY KEY,
    name     text NOT NULL UNIQUE,            -- канон: 'Nike'
    aliases  text[] NOT NULL DEFAULT '{}'     -- {'найк','найки','nike','nk'}
);

CREATE TYPE item_category AS ENUM (
  'sneakers','shoes','boots','slides',
  'tshirt','longsleeve','hoodie','sweatshirt','jacket','coat','pants','shorts','jeans',
  'cap','bag','accessory','other'
);

ALTER TABLE products
    ADD COLUMN brand_id     int REFERENCES brands(id),
    ADD COLUMN model        text,                 -- 'Air Force 1 07'
    ADD COLUMN colorway     text,                 -- 'White/White'
    ADD COLUMN style_code   text,                 -- 'CW2288-111' — золотой ключ для кроссовок
    ADD COLUMN category     item_category,
    ADD COLUMN gender       text,                 -- 'men','women','unisex','kids'
    ADD COLUMN colors       text[] NOT NULL DEFAULT '{}',  -- нормализованные: {'white'}
    ADD COLUMN materials    text[] NOT NULL DEFAULT '{}',
    ADD COLUMN season       text,
    ADD COLUMN price        numeric(12,2),
    ADD COLUMN attrs        jsonb NOT NULL DEFAULT '{}',   -- всё остальное от VLM
    ADD COLUMN search_text  text NOT NULL DEFAULT '',      -- собирается в коде, см. §4.3
    ADD COLUMN text_emb     vector(1024),
    ADD COLUMN index_status text NOT NULL DEFAULT 'pending', -- pending|ready|error
    ADD COLUMN updated_at   timestamptz NOT NULL DEFAULT now();

ALTER TABLE products ADD COLUMN search_tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple',  coalesce(style_code,'') || ' ' || coalesce(sku,'')), 'A') ||
    setweight(to_tsvector('russian', search_text), 'B') ||
    setweight(to_tsvector('simple',  search_text), 'C')
) STORED;

CREATE INDEX ix_products_tsv        ON products USING gin (search_tsv);
CREATE INDEX ix_products_trgm       ON products USING gin (search_text gin_trgm_ops);
CREATE INDEX ix_products_style_code ON products (seller_id, upper(style_code));
CREATE INDEX ix_products_text_emb   ON products USING hnsw (text_emb vector_cosine_ops);

CREATE TABLE product_variants (
    id          serial PRIMARY KEY,
    product_id  int NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    size_label  text NOT NULL,          -- как ввёл продавец: '42', 'US 9', 'M', 'one size'
    size_eu     numeric(4,1),           -- нормализованный для обуви
    size_alpha  text,                   -- XS..XXXL для одежды
    barcode     text,
    sku         text,
    quantity    int NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    price       numeric(12,2),          -- если цена отличается от products.price
    UNIQUE (product_id, size_label)
);
CREATE INDEX ix_variants_product ON product_variants (product_id) WHERE quantity > 0;
CREATE INDEX ix_variants_barcode ON product_variants (barcode);

CREATE TABLE product_photos (
    id           serial PRIMARY KEY,
    product_id   int NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    sha256       char(64) NOT NULL,          -- ключ в /data/photos/ab/cd/<sha>.jpg
    tg_file_id   text,                       -- кэш для повторной отправки, НЕ источник правды
    kind         text NOT NULL DEFAULT 'main', -- main|side|sole|label|box|detail|query
    phash        bigint,                     -- быстрый дедуп точных копий
    is_reference boolean NOT NULL DEFAULT true, -- false для фото из подтверждённых запросов
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (product_id, sha256)
);

CREATE TABLE photo_embeddings (
    photo_id  int NOT NULL REFERENCES product_photos(id) ON DELETE CASCADE,
    model     text NOT NULL,                 -- 'siglip2-base-256' — позволяет переиндексацию
    emb       vector(768) NOT NULL,
    PRIMARY KEY (photo_id, model)
);
CREATE INDEX ix_photo_emb ON photo_embeddings USING hnsw (emb vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

CREATE TABLE search_log (
    id                bigserial PRIMARY KEY,
    seller_id         int NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    query_type        text NOT NULL,        -- text|voice|photo|inline
    query_text        text,
    query_photo_sha   char(64),
    parsed            jsonb,                -- результат парсера запроса
    result_ids        int[] NOT NULL,
    chosen_product_id int,                  -- клик «Это оно»
    latency_ms        int,
    created_at        timestamptz NOT NULL DEFAULT now()
);
```

Замечания:
- `products.quantity` после миграции становится вычисляемым: `sum(variants.quantity)`.
  Сам столбец не удаляем, пока не переделаны хендлеры. Данные переносим как
  `INSERT INTO product_variants (product_id, size_label, quantity) SELECT id, 'one size', quantity FROM products`.
- Postgres-образ меняем на `pgvector/pgvector:pg16`.
- Мультитенантность: все поисковые запросы фильтруются по `seller_id`. Каталоги
  маленькие, поэтому HNSW + фильтр включаем вместе с
  `SET hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8). Без этого фильтр
  после ANN может вернуть меньше `LIMIT` строк.

---

## 4. Пайплайн наполнения (ingest)

### 4.1 UX в боте

```
/add  →  «Пришлите 1–6 фото: общий вид, бок, подошва, бирка, этикетка коробки»
      →  (альбом — собираем media_group_id, ждём 1.5 с)
      →  [можно голосом: «найк форсы белые, сорок второй два штуки, сорок третий один»]
      →  бот: VLM-извлечение + OCR → черновик карточки
         ┌──────────────────────────────────────────┐
         │ Nike Air Force 1 '07 · White/White        │
         │ Код: CW2288-111 · Кроссовки · Мужские     │
         │ Размеры: 42 ×2, 43 ×1                     │
         │ ⚠ Похожая позиция уже есть: #118 (0.93)  │
         └──────────────────────────────────────────┘
         [✅ Сохранить] [➕ К #118 как размеры] [✏️ Бренд/модель] [✏️ Размеры] [✖]
      →  сохранить → index_status='pending' → воркер считает эмбеддинги
```

Ввод размеров одной строкой: `40:1 41:2 42:3`, `S-2 M-3 L-1`, `42.5x2`. Парсер на
regex, неразобранное идёт в LLM.

### 4.2 VLM-извлечение (structured output)

Один вызов на все фото альбома (2–6 изображений в одном сообщении) + транскрипт
голоса. Ответ строго в JSON по схеме (`response_format: json_schema`, а для
моделей без него — JSON mode с валидацией через pydantic и одним retry).

```python
# bot/services/extraction.py
from pydantic import BaseModel, Field

class ExtractedItem(BaseModel):
    category: str = Field(description="одно из item_category")
    brand: str | None
    model: str | None = Field(description="модель без бренда: 'Air Force 1 07', 'Samba OG'")
    colorway: str | None = Field(description="официальное название расцветки, если видно")
    style_code: str | None = Field(description="артикул производителя с бирки/коробки, напр. CW2288-111, B75806")
    colors: list[str] = Field(description="основные цвета, англ., из словаря: black,white,grey,beige,brown,red,...")
    materials: list[str] = []
    gender: str | None
    sizes_seen: list[str] = Field(default=[], description="размеры, прочитанные с бирки/коробки")
    visible_text: list[str] = Field(default=[], description="весь читаемый текст: логотипы, бирки")
    distinctive: list[str] = Field(default=[], description="отличительные детали: 'замшевые вставки', 'gum-подошва'")
    title_ru: str = Field(description="короткое название для карточки на русском")
    confidence: float
```

Промпт (system):

```text
Ты каталогизатор магазина одежды и кроссовок. По фото (могут быть: общий вид, бок,
подошва, бирка внутри язычка, этикетка коробки) и комментарию продавца заполни JSON.
Правила:
- style_code переписывай символ в символ только если он реально виден на бирке/коробке; не угадывай.
- Если бренд/модель не уверен — null, не выдумывай.
- colors — только из словаря: black, white, grey, beige, cream, brown, red, burgundy,
  pink, orange, yellow, green, olive, khaki, blue, navy, lightblue, purple, multi.
- Комментарий продавца приоритетнее того, что видно на фото.
```

Модель берётся из `VISION_MODEL` (env): для Groq —
`meta-llama/llama-4-scout-17b-16e-instruct`, локально — `qwen2.5vl:7b` через
Ollama. Актуальность ID проверять в консоли провайдера.

### 4.3 Каноничный документ позиции (`search_text`)

Собирается детерминированно в коде при каждом изменении карточки. По нему строятся
и `tsvector`, и `text_emb`:

```python
def build_search_text(p: Product, brand: Brand | None, sizes: list[str]) -> str:
    parts = [
        brand.name if brand else "",
        " ".join(brand.aliases) if brand else "",   # 'найк найки nike'
        p.model or "", p.colorway or "", p.style_code or "", p.sku or "",
        CATEGORY_RU[p.category], *CATEGORY_SYNONYMS[p.category],  # 'кроссовки кеды sneakers'
        *[COLOR_RU[c] for c in p.colors], *p.colors,               # 'белый white'
        *p.materials, GENDER_RU.get(p.gender, ""),
        p.name, p.description or "",
        *p.attrs.get("distinctive", []),
        *p.attrs.get("visible_text", []),
        "размеры " + " ".join(sizes),
    ]
    return normalize(" ".join(x for x in parts if x))  # lower, ё→е, схлопнуть пробелы
```

Ключевой момент: алиасы брендов, русские и английские цвета и синонимы категорий
лежат в документе, а не в запросе. Тогда расширять запрос почти не нужно, и
лексический поиск становится двуязычным.

Словари (`bot/catalog/dictionaries.py`) задаём вручную, стартовый набор — около
50 брендов: Nike/найк, Jordan/джорданы/жорданы, Adidas/адик/адидас,
New Balance/нб/нью беланс, Asics/асикс, Stone Island/стоник/стонайленд,
The North Face/тнф, Carhartt/кархарт, Salomon/саломон и т. д. Модельные алиасы:
«форсы» → Air Force 1, «данки» → Dunk, «ябы»/«изи» → Yeezy, «самбы» → Samba.

### 4.4 Фото

1. `bot.download()` → bytes → `sha256` → `/data/photos/ab/cd/<sha>.jpg`
   (ресайз до 1600px по длинной стороне, JPEG q=88, EXIF удаляем).
2. `phash` (imagehash) — для мгновенного «это фото уже есть».
3. Задача в очередь: эмбеддинг SigLIP2 → `photo_embeddings`.
4. Дедуп при добавлении: kNN по эмбеддингу; если `distance < 0.08` с позицией того же
   продавца, предлагаем «добавить как размеры к #N».
5. Обрезка фона (rembg) **не нужна** на старте. Её включаем, только если eval
   (§7) покажет, что фон (полка, коробка, руки) ломает выдачу.

### 4.5 Фоновый индексатор

```python
# bot/workers/indexer.py — запускается тем же процессом как asyncio-task
CLAIM = text("""
    UPDATE products SET index_status = 'running'
    WHERE id IN (SELECT id FROM products WHERE index_status = 'pending'
                 ORDER BY updated_at LIMIT 16 FOR UPDATE SKIP LOCKED)
    RETURNING id
""")
# для каждого id: search_text → bge-m3 → text_emb; новые фото → SigLIP2 → photo_embeddings
# ошибка → index_status='error', attrs->'index_error'; ретрай по /reindex
```

---

## 5. Пайплайн поиска

### 5.1 Разбор запроса

```
вход: текст | голос (Whisper → текст) | фото (+ подпись)
        │
        ▼
┌─ быстрые правила (regex, без LLM) ──────────────────────────────┐
│ style_code  [A-Z]{1,3}\d{3,5}-\d{3} | [A-Z]{2}\d{4} → exact      │
│ barcode     \d{8,14} → exact по variants                         │
│ avito URL   → уже есть                                           │
│ размер      «42», «42.5», «42,5», «US 9», «27 см», «XL», «р. 44» │
│ цена        «до 10к», «до 12 000»                                │
│ цвет/бренд  словари §4.3                                         │
└──────────────────────────────────────────────────────────────────┘
        │ остаток текста = семантическая часть
        ▼
 LLM-парсер — только если правила ничего не извлекли и запрос > 4 слов
 (дешёвая модель, json_schema: {brand, category, colors, size, max_price, free_text})
```

Exact-попадание (style_code, barcode, SKU) возвращается сразу, без ранжирования.

### 5.2 Текстовый гибридный поиск (одним SQL)

```sql
-- :seller, :q (нормализованный текст), :qv (bge-m3 запроса), :size_eu, :size_alpha
WITH
fts AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(search_tsv, q) DESC) AS r
  FROM products, websearch_to_tsquery('russian', :q) q
  WHERE seller_id = :seller AND search_tsv @@ q
  ORDER BY ts_rank_cd(search_tsv, q) DESC LIMIT 50
),
trg AS (
  SELECT id, row_number() OVER (ORDER BY sim DESC) AS r FROM (
    SELECT id, word_similarity(:q, search_text) AS sim
    FROM products WHERE seller_id = :seller AND :q <% search_text
    ORDER BY sim DESC LIMIT 50) t
),
vec AS (
  SELECT id, row_number() OVER (ORDER BY d) AS r FROM (
    SELECT id, text_emb <=> :qv AS d
    FROM products WHERE seller_id = :seller AND text_emb IS NOT NULL
    ORDER BY d LIMIT 50) t
),
fused AS (
  SELECT id, sum(1.0 / (60 + r)) AS score
  FROM (SELECT * FROM fts UNION ALL SELECT * FROM trg UNION ALL SELECT * FROM vec) u
  GROUP BY id
)
SELECT p.*, f.score,
       coalesce(json_agg(json_build_object('size', v.size_label, 'qty', v.quantity)
                ORDER BY v.size_eu NULLS LAST, v.size_label)
                FILTER (WHERE v.quantity > 0), '[]') AS stock
FROM fused f
JOIN products p ON p.id = f.id
LEFT JOIN product_variants v ON v.product_id = p.id
WHERE (:size_eu::numeric IS NULL OR EXISTS (
         SELECT 1 FROM product_variants x
         WHERE x.product_id = p.id AND x.quantity > 0 AND x.size_eu = :size_eu))
  AND (:size_alpha::text IS NULL OR EXISTS (
         SELECT 1 FROM product_variants x
         WHERE x.product_id = p.id AND x.quantity > 0 AND x.size_alpha = :size_alpha))
GROUP BY p.id, f.score
ORDER BY f.score DESC
LIMIT 10;
```

Размер — это жёсткий фильтр по наличию. Если с фильтром выдача пустая,
повторяем без него и показываем «в 42 нет, есть 41, 43».

### 5.3 Поиск по фото

Сигналов три, все сливаются через RRF:

| Сигнал | Как | Вес/роль |
|---|---|---|
| **Визуальный kNN** | SigLIP2(фото запроса) → `photo_embeddings`, агрегат `min(distance)` по `product_id` | основной; работает на любом ракурсе |
| **Exact по OCR** | VLM извлекает `style_code`/бренд/модель с фото (тот же `ExtractedItem`) → exact или текстовый гибрид §5.2 | решает «тот же силуэт, другая расцветка», если видна бирка/коробка |
| **Подпись к фото** | `message.caption` → парсер §5.1 (размер, цвет) | фильтры |

```sql
-- визуальная ветка
SELECT product_id, row_number() OVER (ORDER BY d) AS r FROM (
  SELECT ph.product_id, min(e.emb <=> :img_v) AS d
  FROM photo_embeddings e
  JOIN product_photos ph ON ph.id = e.photo_id
  JOIN products p ON p.id = ph.product_id
  WHERE p.seller_id = :seller AND e.model = 'siglip2-base-256'
  GROUP BY ph.product_id
  ORDER BY d LIMIT 30) t;
```

Для большого каталога вложенный ANN: сначала `ORDER BY emb <=> :img_v LIMIT 200`
по индексу, потом `GROUP BY`.

VLM-ветка медленнее (1–3 с), поэтому делаем двухфазный ответ:
1. Через ~300 мс отправляем визуальный top-5 (сообщение «🔎 Похожие:»).
2. VLM-ветка завершилась и дала exact или изменила top-1 — редактируем сообщение
   (`edit_message_text`/`edit_message_media`).

**Пороги уверенности** (cosine distance SigLIP2, калибруются на eval §7, стартовые):
- `d < 0.10` и отрыв от второго > 0.04 → «✅ Это, скорее всего, #118» + остатки;
- `d < 0.25` → «Похожие позиции» (top-5);
- иначе → «Точных совпадений нет, вот ближайшие» + кнопка «Уточнить текстом».

### 5.4 Выдача в Telegram

- Top-1 — `send_photo` с карточкой и остатками: `42 ×2 · 43 ×1 · 44 —`.
- Top-2..5 — `send_media_group` (до 10) с подписями `#id бренд модель`, а под ним
  сообщение с инлайн-кнопками `[#118] [#204] [#77] …` → карточка.
- Кнопки на карточке: `[✅ Это оно]` (пишет `chosen_product_id`),
  `[−1 шт.]` (продажа), `[Показать все фото]`, `[Отправить клиенту]`
  (inline-share).
- Повторная отправка фото идёт через кэшированный `tg_file_id`. Если он
  невалиден (сменился токен), загружаем из `/data/photos` и обновляем кэш.

### 5.5 Inline-режим (поиск с телефона в любом чате)

`@sklad_bot форсы 42` в чате с клиентом → `InlineQueryResultCachedPhoto` с
карточкой и наличием. Тот же гибридный поиск, только текст, `cache_time=5`,
`is_personal=True`. Включается в BotFather: `/setinline`.
Это закрывает сценарий «клиент спросил в личке, продавец за 2 секунды ответил
фоткой из базы».

### 5.6 Telegram Mini App (этап 4)

Нужен для того, что неудобно в чате: сетка фото, фильтры (бренд, размер, цвет,
цена), камера с подсказкой «наведите на этикетку коробки». Фронт — статический
(Vite + vanilla/Preact) за Traefik, бэкенд — FastAPI в том же контейнере, что и
бот. Авторизация по `initData` (HMAC-SHA256 от `BOT_TOKEN`). Камера доступна
через `<input type="file" accept="image/*" capture="environment">`.

---

## 6. Сервис эмбеддингов

Отдельный контейнер: torch-зависимости не тащим в образ бота, и сервис можно
перенести на машину с GPU.

```python
# embedder/app.py
import io
import torch
from fastapi import FastAPI, UploadFile
from PIL import Image
from pydantic import BaseModel
from transformers import AutoModel, AutoProcessor

MODEL_ID = "google/siglip2-base-patch16-256"
torch.set_num_threads(4)
model = AutoModel.from_pretrained(MODEL_ID).eval()
proc = AutoProcessor.from_pretrained(MODEL_ID)
app = FastAPI()


class TextIn(BaseModel):
    texts: list[str]


def _norm(x: torch.Tensor) -> list[list[float]]:
    return torch.nn.functional.normalize(x, dim=-1).tolist()


@app.post("/v1/image")
async def embed_image(files: list[UploadFile]):
    imgs = [Image.open(io.BytesIO(await f.read())).convert("RGB") for f in files]
    with torch.inference_mode():
        x = model.get_image_features(**proc(images=imgs, return_tensors="pt"))
    return {"model": "siglip2-base-256", "vectors": _norm(x)}


@app.post("/v1/clip-text")  # текст → пространство изображений: «красные кроссовки» → фото
async def embed_clip_text(body: TextIn):
    inp = proc(text=body.texts, padding="max_length", max_length=64, return_tensors="pt")
    with torch.inference_mode():
        x = model.get_text_features(**inp)
    return {"model": "siglip2-base-256", "vectors": _norm(x)}


@app.get("/health")
def health():
    return {"ok": True}
```

```dockerfile
# embedder/Dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "transformers>=4.49" pillow fastapi uvicorn[standard] python-multipart sentencepiece
ENV HF_HOME=/models
COPY app.py /app/app.py
WORKDIR /app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
```

bge-m3 берём из имеющейся Ollama (`POST /api/embed`, `model: bge-m3`): отдельный
сервис не нужен.

Производительность на CPU (4 vCPU LXC): SigLIP2-base ~120–200 мс на изображение,
батч из 6 фото ~0.6 с. Для склада на тысячи позиций этого достаточно.

### docker-compose (дельта)

```yaml
services:
  db:
    image: pgvector/pgvector:pg16          # было postgres:16-alpine

  embedder:
    build: ./embedder
    volumes:
      - hf_models:/models
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8080/health')"]
      interval: 10s
      start_period: 120s                   # первая загрузка весов
    restart: unless-stopped

  bot:
    environment:
      EMBEDDER_URL: http://embedder:8080
      OLLAMA_URL: http://ollama.lan:11434
      PHOTOS_DIR: /data/photos
    volumes:
      - photos:/data/photos
    depends_on:
      embedder:
        condition: service_healthy

volumes:
  hf_models:
  photos:
```

### Новые настройки (`bot/config.py`)

```python
EMBEDDER_URL: str = "http://embedder:8080"
OLLAMA_URL: str = "http://localhost:11434"
TEXT_EMBED_MODEL: str = "bge-m3"
VISION_PROVIDER: str = "groq"          # groq | openrouter | ollama
VISION_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"
PHOTOS_DIR: str = "/data/photos"
PHOTO_MATCH_STRONG: float = 0.10
PHOTO_MATCH_WEAK: float = 0.25
```

---

## 7. Качество: как понять, что поиск работает

Без eval пороги из §5.3 — гадание. Минимальный набор:

1. **Датасет**: 100–200 реальных запросов. Это фото, которые присылали клиенты
   (скрины с Авито, фото «с улицы», фото в руке), плюс 50 текстовых запросов в
   живом стиле. Для каждого — правильный `product_id`. Хранится как
   `eval/queries.jsonl` + `eval/photos/`.
2. **Метрики**: `recall@1`, `recall@5`, `MRR`, p95 latency. Отдельно по типам
   (фото, текст, голос) и по категориям (обувь и одежда ведут себя по-разному).
3. **Скрипт**: `python -m eval.run --db $DATABASE_URL`. Прогоняет запросы через тот
   же `search_service`, печатает таблицу и строки с ошибками.
4. **Онлайн-сигнал**: `search_log.chosen_product_id`, доля кликов «Это оно» в
   top-1 и top-5.
5. **Петля обучения без обучения**: фото запроса, которое продавец подтвердил
   кнопкой «Это оно», сохраняем в `product_photos` с `kind='query'`,
   `is_reference=false` и тоже индексируем. Каталог сам набирает фото «как
   снимают клиенты», и recall на реальных запросах растёт без файнтюна.

Ориентиры, на которые стоит выйти до этапа 4: фото → `recall@5 ≥ 0.9`,
текст → `recall@5 ≥ 0.85`, p95 < 1.5 с (визуальная ветка без VLM < 500 мс).

Если визуальный recall упирается в потолок, рычаги в порядке цены:
больше ракурсов в каталоге → фото-запросы в индексе (п. 5) → rembg/кроп по
детектору → `siglip2-large` → файнтюн проекционного слоя на парах из `search_log`.

---

## 8. Тесты

Сейчас тесты гоняются на `sqlite+aiosqlite:///:memory:`, а pgvector, `tsvector`
и `pg_trgm` там нет. Предлагается:
- юнит-тесты парсеров (размеры, style_code, цена, алиасы) и `build_search_text`
  оставить на чистом Python;
- поисковые SQL — в `tests/integration/` с маркером `@pytest.mark.pg`, на
  `pgvector/pgvector:pg16` из compose (`docker compose run --rm bot pytest -m pg`);
- embedder и LLM в тестах — фейки с детерминированными векторами (hash → vector).

---

## 9. План работ

| Этап | Что | Результат | Оценка |
|---|---|---|---|
| **0. Быстрый фикс** | VLM в фото-поиске возвращает JSON (`ExtractedItem`); ищем по `brand + model + style_code`, а не по абзацу; `VISION_MODEL` в конфиг | фото-поиск начинает что-то находить на текущей схеме | 0.5 дня |
| **1. Схема и текст** | миграция 0002, `pgvector` образ, варианты/размеры, `product_photos` + файловое хранилище, словари, `build_search_text`, гибридный SQL §5.2, парсер запроса §5.1, перенос существующих данных | нормальный текстовый и голосовой поиск с фильтром по размеру | 3–4 дня |
| **2. Визуальный поиск** | embedder-сервис, индексатор, визуальная ветка §5.3, двухфазный ответ, пороги | поиск по фото клиента | 2–3 дня |
| **3. Наполнение** | `/add` с альбомом, VLM-черновик карточки, ввод размеров строкой, дедуп, `/reindex` | добавление позиции за ~30 с с телефона | 2–3 дня |
| **4. Каналы и качество** | inline-режим, кнопки «Это оно / −1 шт.», `search_log`, eval-скрипт, query-фото в индекс; опционально Mini App | измеримое качество, поиск из любого чата | 3–5 дней |

Порядок важен: этап 1 закладывает схему, на которой строится всё остальное.
Этап 0 — временная заплатка, чтобы фото-поиск можно было показать уже сейчас.
