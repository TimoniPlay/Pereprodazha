"""
Автономний AI-агент для моніторингу OLX та перепродажу смартфонів.

Зміни порівняно зі старою версією:
  * RSS на OLX більше не працює -> тепер використовується JSON API сайту.
  * Секрети (токен Telegram, ключ ШІ) беруться ТІЛЬКИ зі змінних середовища / .env.
  * ШІ-провайдер вибирається через LLM_PROVIDER (groq/openrouter/openai/deepseek/anthropic).
  * feedparser більше не потрібен.
"""
# pyright: reportMissingImports=false
import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
from collections import defaultdict

import aiohttp
import aiosqlite
import requests

try:
    from dotenv import load_dotenv  # pip install python-dotenv (необов'язково)
    load_dotenv()
except ImportError:
    pass

try:
    import cloudscraper  # type: ignore[import-not-found]
    scraper = cloudscraper.create_scraper()
except ImportError:
    scraper = None

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.markdown import hlink

# ============================================================
# КОНФІГУРАЦІЯ
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8954609177:AAGWaI3BXF113WVmUqdxJp-QUnrflYqCi2E").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004466348374").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "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").strip()

# Провайдер ШІ: groq | openrouter | openai | deepseek | anthropic
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()

# (url, модель за замовчуванням, формат API)
LLM_PRESETS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b", "openai"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "meta-llama/llama-3.3-70b-instruct:free", "openai"),
    "openai":     ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini", "openai"),
    "deepseek":   ("https://api.deepseek.com/chat/completions", "deepseek-chat", "openai"),
    "anthropic":  ("https://api.anthropic.com/v1/messages", "claude-haiku-4-5-20251001", "anthropic"),
}
if LLM_PROVIDER not in LLM_PRESETS:
    sys.exit(f"Невідомий LLM_PROVIDER='{LLM_PROVIDER}'. Доступні: {', '.join(LLM_PRESETS)}")

_preset_url, _preset_model, LLM_API_FORMAT = LLM_PRESETS[LLM_PROVIDER]
LLM_API_URL = os.getenv("LLM_API_URL", _preset_url)     # можна перевизначити
LLM_MODEL = os.getenv("LLM_MODEL", _preset_model)       # можна перевизначити

if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and LLM_API_KEY):
    sys.exit(
        "Не задано TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / LLM_API_KEY.\n"
        "Створіть файл .env поруч із main.py:\n"
        "  TELEGRAM_BOT_TOKEN=...\n  TELEGRAM_CHAT_ID=-100...\n"
        "  LLM_PROVIDER=groq\n  LLM_API_KEY=..."
    )

TELEGRAM_CHAT_ID = int(TELEGRAM_CHAT_ID)

POLL_INTERVAL_SECONDS = 120          # Пауза між колами (2 хв)
REQUEST_DELAY = (2.0, 4.0)           # Пауза між запитами до OLX
DB_PATH = "listings.db"
SEEN_TTL_DAYS = 30
MAX_PRICE_GLOBAL = 13000
SUMMARY_MAX_CHARS = 1500
MAX_LLM_FAILS = 3
LLM_MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "3"))  # сек між запитами до ШІ
RATE_LIMIT_PAUSE = 60                # пауза, якщо ШІ віддав 429
OLX_LIMIT = 40                       # Скільки найновіших оголошень брати на запит

SEARCH_QUERIES = [
    "pixel 7", "pixel 7a", "pixel 7 pro",
    "pixel 8", "pixel 8a", "pixel 8 pro",
    "pixel 9", "pixel 9 pro"
]

OLX_API_URL = "https://www.olx.ua/api/v1/offers/"

ACCESSORY_RE = re.compile(
    r"^\s*(чохол|чехол|захисне скло|защитное стекло|скло|плівка|кабель|зарядн)",
    re.IGNORECASE,
)

# Заголовок має містити одну з цільових моделей (Pixel 4/5/XL, Samsung тощо відсікаються)
TARGET_MODEL_RE = re.compile(r"(pixel\s*[6-9]\s*(?:a|pro)?(?![a-z0-9])|iphone\s*1[12](?!\d))", re.IGNORECASE)

# Аксесуари/запчастини: ці слова відсікають оголошення будь-де в заголовку
ACCESSORY_ANY_RE = re.compile(
    r"(чохол|чехол|бампер|захисне скло|защитное стекло|скло\b|стекло\b|плівк|плёнк|"
    r"кабель|зарядк|зарядн|хаб\b|перехідник|адаптер|підставк|stand\b|case\b|"
    r"іграшк|игрушк|шлейф|ноутбук|laptop)",
    re.IGNORECASE,
)
# Слова, що означають запчастину, якщо стоять на початку заголовка
ACCESSORY_START_RE = re.compile(
    r"^\W*(?:\S+\s+){0,2}?(екран|дисплей|модуль|тачскр|корпус|акумулятор|аккумулятор|батаре)",
    re.IGNORECASE,
)


def is_relevant(title: str) -> bool:
    if not TARGET_MODEL_RE.search(title):
        return False
    if ACCESSORY_ANY_RE.search(title) or ACCESSORY_START_RE.match(title):
        return False
    return True


HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.olx.ua/",
}

# ============================================================
# БАЗА ДАНИХ
# ============================================================


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn: aiosqlite.Connection | None = None

    async def init(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("""CREATE TABLE IF NOT EXISTS seen_listings (
            id TEXT PRIMARY KEY,
            seen_at INTEGER NOT NULL
        )""")
        await self.conn.execute("""CREATE TABLE IF NOT EXISTS deals (
            key TEXT PRIMARY KEY,
            title TEXT,
            link TEXT,
            price INTEGER,
            status TEXT DEFAULT 'new'
        )""")
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def is_seen(self, listing_id: str) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM seen_listings WHERE id=?", (listing_id,))
        return await cur.fetchone() is not None

    async def mark_seen(self, listing_id: str):
        await self.conn.execute(
            "INSERT OR IGNORE INTO seen_listings (id, seen_at) VALUES (?, ?)",
            (listing_id, int(time.time())),
        )
        await self.conn.commit()

    async def cleanup(self):
        threshold = int(time.time()) - SEEN_TTL_DAYS * 86400
        await self.conn.execute("DELETE FROM seen_listings WHERE seen_at < ?", (threshold,))
        await self.conn.commit()

    async def save_deal(self, key: str, title: str, link: str, price: int | None):
        await self.conn.execute(
            "INSERT OR REPLACE INTO deals (key, title, link, price) VALUES (?, ?, ?, ?)",
            (key, title, link, price),
        )
        await self.conn.commit()

    async def get_deal(self, key: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT title, link, price, status FROM deals WHERE key=?", (key,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {"title": row[0], "link": row[1], "price": row[2], "status": row[3]}

    async def set_status(self, key: str, status: str):
        await self.conn.execute("UPDATE deals SET status=? WHERE key=?", (status, key))
        await self.conn.commit()


db = Database()

# ============================================================
# СКРАПЕР OLX (JSON API)
# ============================================================


def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def _get_olx_json(query: str) -> dict:
    """Синхронний запит до OLX API (викликається через asyncio.to_thread)."""
    params = {
        "query": query,
        "sort_by": "created_at:desc",
        "limit": OLX_LIMIT,
        "offset": 0,
    }
    try:
        client = scraper or requests
        resp = client.get(OLX_API_URL, params=params, headers=HTTP_HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logging.warning(
            f"OLX HTTP {resp.status_code} для '{query}'. "
            f"Початок відповіді: {resp.text[:150]!r}"
        )
    except Exception as e:
        logging.error(f"Помилка мережі/JSON для '{query}': {e}")
    return {}


def _extract_api_price(offer: dict) -> int | None:
    for p in offer.get("params", []) or []:
        if p.get("key") == "price":
            value = p.get("value") or {}
            currency = value.get("currency")
            amount = value.get("value")
            if amount is not None and currency in (None, "UAH"):
                try:
                    return int(float(amount))
                except (TypeError, ValueError):
                    return None
    return None


async def fetch_offers(query: str) -> list[dict]:
    data = await asyncio.to_thread(_get_olx_json, query)
    listings = []
    for o in data.get("data", []) or []:
        offer_id = o.get("id")
        link = o.get("url", "")
        if offer_id is None or not link:
            continue
        listings.append({
            "id": str(offer_id),
            "title": clean_text(o.get("title", "")),
            "link": link,
            "summary": clean_text(o.get("description", ""))[:SUMMARY_MAX_CHARS],
            "published": o.get("created_time", ""),
            "price_uah": _extract_api_price(o),
        })
    return listings


PRICE_PATTERNS = [
    re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+|\d{3,})\s*(?:грн|₴|uah)", re.IGNORECASE),
    re.compile(r"ціна[:\s]*(\d{1,3}(?:[ \u00a0]\d{3})+|\d{3,})", re.IGNORECASE),
]


def extract_price(title: str, summary: str) -> int | None:
    """Запасний варіант: шукаємо ціну в тексті, якщо API її не віддав."""
    text = f"{title} {summary}"
    for pattern in PRICE_PATTERNS:
        m = pattern.search(text)
        if m:
            price = int(re.sub(r"\D", "", m.group(1)))
            if 500 <= price <= 200000:
                return price
    return None

# ============================================================
# AI-АНАЛІЗАТОР
# ============================================================

SYSTEM_PROMPT = """Ти — експерт з аналізу оголошень смартфонів для перепродажу в Україні.
Проаналізуй оголошення та поверни СТРОГО JSON:

{
  "model": "string",
  "price_uah": number | null,
  "max_allowed_price": number,
  "condition_type": "defect" | "ideal",
  "defects_found": ["string"],
  "is_neverlock": true | false,
  "fatal_flaws": ["string"],
  "verdict": "IGNORE" | "GOOD" | "HOT_DEAL",
  "reasoning": "string",
  "suggested_action": "string"
}

ПРАВИЛА ЦІН:
- Pixel 6/6a/6 Pro: до 4000 грн.
- Pixel 7/7a/7 Pro: дефект до 6500 грн; ідеал до 8000 грн.
- Pixel 8/8a/8 Pro: дефект до 7000 грн; ідеал до 9000 грн.
- Pixel 9/9a: дефект до 11000 грн; ідеал до 13000 грн.
- iPhone 11 / 12: ТІЛЬКИ без дефектів — до 4000 грн.

ВЕРДИКТИ:
- IGNORE — є стоп-фактор, ціна вища за максимум, або не Neverlock для Pixel.
- GOOD — нормальна ціна в межах ліміту.
- HOT_DEAL — ціна на 20%+ нижча від максимуму.

Відповідай ТІЛЬКИ JSON.
Усі текстові поля (reasoning, suggested_action, defects_found, fatal_flaws) пиши УКРАЇНСЬКОЮ мовою.
Ключі JSON та значення verdict (IGNORE, GOOD, HOT_DEAL) залишай без змін, англійською."""


_llm_lock = asyncio.Lock()
_llm_last_call = 0.0


async def _throttle():
    """Не частіше ніж раз на LLM_MIN_INTERVAL секунд."""
    global _llm_last_call
    async with _llm_lock:
        wait = LLM_MIN_INTERVAL - (time.monotonic() - _llm_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _llm_last_call = time.monotonic()


async def _call_llm(session: aiohttp.ClientSession, user_content: str) -> str:
    """Один запит до ШІ. Повертає текст відповіді. Кидає RateLimited при 429."""
    if LLM_API_FORMAT == "anthropic":
        headers = {
            "x-api-key": LLM_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": LLM_MODEL,
            "max_tokens": 1000,
            "temperature": 0.1,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        }
    else:  # OpenAI-сумісний (Groq, OpenRouter, OpenAI, DeepSeek)
        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": LLM_MODEL,
            "temperature": 0.1,
            "max_tokens": 2000,
            "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }

    async with session.post(
        LLM_API_URL, json=payload, headers=headers,
        timeout=aiohttp.ClientTimeout(total=45),
    ) as resp:
        if resp.status == 429:
            body = (await resp.text())[:300].replace("\n", " ")
            raise RateLimited(body)
        if resp.status >= 400:
            body = (await resp.text())[:300].replace("\n", " ")
            raise RuntimeError(f"HTTP {resp.status}: {body}")
        data = await resp.json()

    if LLM_API_FORMAT == "anthropic":
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    return data["choices"][0]["message"]["content"]


class RateLimited(Exception):
    pass


async def analyze_listing(session: aiohttp.ClientSession, listing: dict) -> dict | None:
    """Повертає вердикт, None (збій) або {"_rate_limited": True} (429)."""
    user_content = f"""Оголошення:
Заголовок: {listing.get('title', '')}
Ціна: {listing.get('price_uah') or 'не вказано'} грн
Опис: {listing.get('summary', '')}
"""
    for attempt in range(3):
        try:
            await _throttle()
            content = await _call_llm(session, user_content)

            start, end = content.find("{"), content.rfind("}")
            if start == -1 or end == -1:
                raise json.JSONDecodeError("JSON не знайдено", content, 0)

            result = json.loads(content[start:end + 1])
            result["link"] = listing.get("link", "")
            result["raw_title"] = listing.get("title", "")
            return result
        except RateLimited as e:
            logging.warning(f"{LLM_PROVIDER} 429 ({LLM_MODEL}): {e}")
            return {"_rate_limited": True}
        except Exception as e:
            logging.error(f"Спроба ШІ #{attempt + 1} провалилася ({LLM_PROVIDER}/{LLM_MODEL}): {e}")
            await asyncio.sleep(2)
    return None


def is_worth_sending(verdict: dict, price: int | None) -> bool:
    if verdict.get("verdict") not in ("GOOD", "HOT_DEAL"):
        return False
    if verdict.get("fatal_flaws"):
        return False

    model = str(verdict.get("model", "")).lower()
    if "pixel" in model and not verdict.get("is_neverlock"):
        return False

    if price:
        verdict["price_uah"] = price
    real_price = verdict.get("price_uah")
    max_price = verdict.get("max_allowed_price")
    if (isinstance(real_price, (int, float)) and isinstance(max_price, (int, float))
            and max_price > 0 and real_price > max_price):
        return False
    return True

# ============================================================
# TELEGRAM-БОТ
# ============================================================

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


def esc(value) -> str:
    return html.escape(str(value))


def as_list(value) -> list:
    return value if isinstance(value, list) else ([value] if value else [])


def make_key(listing_id: str) -> str:
    return hashlib.sha1(listing_id.encode()).hexdigest()[:16]


def build_card(verdict: dict) -> str:
    emoji = "🔥" if verdict["verdict"] == "HOT_DEAL" else "✅"
    lines = [
        f"{emoji} <b>{esc(verdict.get('model', 'Невідомо'))}</b>",
        f"💰 Ціна: <b>{esc(verdict.get('price_uah') or '?')} грн</b> (макс: {esc(verdict.get('max_allowed_price', '?'))})",
        f"📊 Стан: {esc(verdict.get('condition_type', '?'))}",
        f"🔓 Neverlock: {'✅' if verdict.get('is_neverlock') else '❌'}",
    ]
    defects = as_list(verdict.get("defects_found"))
    if defects:
        lines.append(f"⚠️ Дефекти: {', '.join(map(esc, defects))}")
    lines.append(f"🧠 Висновок ШІ: {esc(str(verdict.get('reasoning', ''))[:500])}")
    lines.append(f"🔗 {hlink('Посилання на OLX', verdict.get('link') or '#')}")
    return "\n".join(lines)


def build_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📞 Текст для торгу", callback_data=f"negotiate:{key}"),
            InlineKeyboardButton(text="✅ Взяти в роботу", callback_data=f"take:{key}"),
        ]
    ])


@dp.callback_query(F.data.startswith("negotiate:"))
async def negotiate_cb(callback: types.CallbackQuery):
    deal = await db.get_deal(callback.data.split(":", 1)[1])
    title = f" «{deal['title']}»" if deal and deal["title"] else ""
    text = f"Вітаю! Бачу ваше оголошення{title}. Чи можлива невелика знижка? Готовий забрати сьогодні!"
    await callback.message.answer(f"📞 <code>{esc(text)}</code>", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("take:"))
async def take_cb(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    deal = await db.get_deal(key)
    if deal and deal["status"] == "taken":
        await callback.answer("Вже в роботу взято", show_alert=False)
        return
    await db.set_status(key, "taken")
    await callback.message.answer("✅ Взято в роботу!")
    await callback.answer()

# ============================================================
# ГОЛОВНИЙ ЦИКЛ
# ============================================================

llm_fails: dict[str, int] = defaultdict(int)


async def process_new_listing(session: aiohttp.ClientSession, listing: dict) -> bool:
    """Повертає True, якщо оголошення опрацьовано і його можна позначити як переглянуте."""
    logging.info(f"🔍 Аналізуємо ШІ: {listing['title']} | {listing.get('price_uah')} грн")
    verdict = await analyze_listing(session, listing)
    if verdict and verdict.get("_rate_limited"):
        logging.warning(f"⏸️ Пауза {RATE_LIMIT_PAUSE} с через ліміт ШІ; оголошення розглянемо пізніше")
        await asyncio.sleep(RATE_LIMIT_PAUSE)
        return False  # НЕ позначаємо як переглянуте і не рахуємо як збій
    if not verdict:
        llm_fails[listing["id"]] += 1
        return llm_fails[listing["id"]] >= MAX_LLM_FAILS

    if not is_worth_sending(verdict, listing.get("price_uah")):
        logging.info(f"⏭️ Пропущено (не підходить): {verdict.get('model')} | {verdict.get('verdict')}")
        return True

    key = make_key(listing["id"])
    await db.save_deal(key, listing["title"], listing["link"], verdict.get("price_uah"))
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=build_card(verdict),
            parse_mode="HTML",
            reply_markup=build_keyboard(key),
            disable_web_page_preview=True,
        )
        logging.info(f"🚀 ВІДПРАВЛЕНО В TG: {verdict.get('model')} за {verdict.get('price_uah')} грн")
    except TelegramAPIError as e:
        logging.error(f"Помилка відправки в Telegram: {e}")
    return True


async def scrape_loop(session: aiohttp.ClientSession):
    logging.info("🚀 Скрапер розпочав роботу...")

    while True:
        try:
            for query in SEARCH_QUERIES:
                logging.info(f"Перевірка OLX запиту: '{query}'...")

                items = await fetch_offers(query)
                logging.info(f"Знайдено оголошень ({query}): {len(items)}")

                for item in items:
                    if await db.is_seen(item["id"]):
                        continue

                    price = item.get("price_uah") or extract_price(item["title"], item["summary"])
                    item["price_uah"] = price

                    if (not is_relevant(item["title"]) or ACCESSORY_RE.match(item["title"])
                            or (price and price > MAX_PRICE_GLOBAL)):
                        await db.mark_seen(item["id"])
                        continue

                    if await process_new_listing(session, item):
                        await db.mark_seen(item["id"])

                await asyncio.sleep(random.uniform(*REQUEST_DELAY))

            await db.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception(f"Помилка в циклі скрапера: {e}")

        logging.info(f"💤 Пауза {POLL_INTERVAL_SECONDS} сек перед наступним колом...")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await db.init()

    async with aiohttp.ClientSession() as session:
        scraper_task = asyncio.create_task(scrape_loop(session))
        logging.info("Бот запущено успішно.")
        try:
            await dp.start_polling(bot)
        finally:
            scraper_task.cancel()
            await asyncio.gather(scraper_task, return_exceptions=True)
            await db.close()
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Зупинено користувачем.")
