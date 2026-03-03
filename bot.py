"""
🇪🇸 Испания Daily — Telegram-бот с ежедневным дайджестом новостей Испании
RSS → Claude API (суммаризация + перевод на русский) → Telegram

v6: + команда /stats только для админа
"""

import os
import re
import json
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ─── Настройки ───────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TIMEZONE = ZoneInfo("Europe/Madrid")
DB_PATH = os.environ.get("DB_PATH", "users.db")
DIGEST_GEN_HOUR = 6
ADMIN_ID = 8023489016  # Telegram ID админа

# ─── 9 категорий ─────────────────────────────────────────────────────────────

CATEGORIES = {
    "politics":     {"emoji": "1️⃣", "name": "Политика",            "short": "Политика"},
    "economy":      {"emoji": "2️⃣", "name": "Экономика и бизнес",  "short": "Экономика"},
    "society":      {"emoji": "3️⃣", "name": "Общество",            "short": "Общество"},
    "local":        {"emoji": "4️⃣", "name": "Барселона / Мадрид",   "short": "Локальные"},
    "incidents":    {"emoji": "5️⃣", "name": "Происшествия",         "short": "Происшествия"},
    "sports":       {"emoji": "6️⃣", "name": "Спорт",               "short": "Спорт"},
    "culture":      {"emoji": "7️⃣", "name": "Культура и афиша",    "short": "Культура"},
    "celebrities":  {"emoji": "8️⃣", "name": "Знаменитости",        "short": "Знаменитости"},
    "humor":        {"emoji": "9️⃣", "name": "Юмор и курьёзы",      "short": "Юмор"},
}

ALL_CATEGORY_KEYS = list(CATEGORIES.keys())

# ─── RSS-источники ───────────────────────────────────────────────────────────

RSS_FEEDS = [
    # 1️⃣ Политика
    ("El País — España", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/espana/portada", "politics"),
    ("El Mundo — España", "https://e00-elmundo.uecdn.es/elmundo/rss/espana.xml", "politics"),
    ("20 Minutos — Nacional", "https://www.20minutos.es/rss/nacional/", "politics"),
    ("El País — Internacional", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/internacional/portada", "politics"),

    # 2️⃣ Экономика
    ("El País — Economía", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/economia/portada", "economy"),
    ("Idealista News", "https://www.idealista.com/news/rss", "economy"),
    ("El Economista", "https://www.eleconomista.es/rss/rss-seleccion-ee.php", "economy"),
    ("Cinco Días", "https://cincodias.elpais.com/rss/portada", "economy"),

    # 3️⃣ Общество
    ("El País — Sociedad", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/sociedad/portada", "society"),
    ("20 Minutos — Sociedad", "https://www.20minutos.es/rss/sociedad/", "society"),
    ("El País — Educación", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/educacion/portada", "society"),
    ("El País — Salud", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/salud/portada", "society"),

    # 4️⃣ Локальные
    ("El País — Madrid", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/madrid/portada", "local"),
    ("El País — Catalunya", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/catalunya/portada", "local"),
    ("20 Minutos — Madrid", "https://www.20minutos.es/rss/madrid/", "local"),
    ("20 Minutos — Barcelona", "https://www.20minutos.es/rss/barcelona/", "local"),

    # 5️⃣ Происшествия
    ("20 Minutos — Sucesos", "https://www.20minutos.es/rss/sucesos/", "incidents"),
    ("El Mundo — Sucesos", "https://e00-elmundo.uecdn.es/elmundo/rss/sucesos.xml", "incidents"),

    # 6️⃣ Спорт
    ("Marca", "https://e00-marca.uecdn.es/rss/portada.xml", "sports"),
    ("AS", "https://feeds.as.com/mrss-s/pages/as/site/as.com/portada", "sports"),
    ("El País — Deportes", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/deportes/portada", "sports"),

    # 7️⃣ Культура
    ("El País — Cultura", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/cultura/portada", "culture"),
    ("20 Minutos — Artes", "https://www.20minutos.es/rss/artes/", "culture"),
    ("El Mundo — Cultura", "https://e00-elmundo.uecdn.es/elmundo/rss/cultura.xml", "culture"),

    # 8️⃣ Знаменитости
    ("20 Minutos — Gente", "https://www.20minutos.es/rss/gente/", "celebrities"),
    ("El Mundo — Loc", "https://e00-elmundo.uecdn.es/elmundo/rss/loc.xml", "celebrities"),

    # 9️⃣ Юмор
    ("20 Minutos — Virales", "https://www.20minutos.es/rss/virales/", "humor"),
    ("El Mundo — Bulos", "https://e00-elmundo.uecdn.es/elmundo/rss/ciencia.xml", "humor"),
]

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── База данных ─────────────────────────────────────────────────────────────


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            digest_hour INTEGER DEFAULT 8,
            digest_minute INTEGER DEFAULT 0,
            subscriptions TEXT DEFAULT '[]',
            is_active INTEGER DEFAULT 1,
            onboarding_done INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS digest_cache (
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            digest_text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, category)
        )
    """)
    conn.commit()
    conn.close()


def upsert_user(chat_id: int, username: str = ""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO users (chat_id, username, subscriptions) VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username, is_active = 1
    """, (chat_id, username, json.dumps(ALL_CATEGORY_KEYS)))
    conn.commit()
    conn.close()


def set_user_time(chat_id: int, hour: int, minute: int = 0):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET digest_hour = ?, digest_minute = ? WHERE chat_id = ?", (hour, minute, chat_id))
    conn.commit()
    conn.close()


def get_user_time(chat_id: int) -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT digest_hour, digest_minute FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row if row else (8, 0)


def get_user_subs(chat_id: int) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT subscriptions FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return ALL_CATEGORY_KEYS.copy()
    return ALL_CATEGORY_KEYS.copy()


def set_user_subs(chat_id: int, subs: list[str]):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET subscriptions = ? WHERE chat_id = ?", (json.dumps(subs), chat_id))
    conn.commit()
    conn.close()


def toggle_user_sub(chat_id: int, category: str) -> list[str]:
    subs = get_user_subs(chat_id)
    if category in subs:
        subs.remove(category)
    else:
        subs.append(category)
    set_user_subs(chat_id, subs)
    return subs


def set_onboarding_done(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET onboarding_done = 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_users_for_hour(hour: int, minute: int = 0) -> list[tuple[int, list[str]]]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT chat_id, subscriptions FROM users WHERE digest_hour = ? AND digest_minute = ? AND is_active = 1",
        (hour, minute),
    ).fetchall()
    conn.close()
    result = []
    for chat_id, subs_json in rows:
        try:
            subs = json.loads(subs_json) if subs_json else ALL_CATEGORY_KEYS.copy()
        except json.JSONDecodeError:
            subs = ALL_CATEGORY_KEYS.copy()
        result.append((chat_id, subs))
    return result


# ─── Кеш дайджестов ──────────────────────────────────────────────────────────


def save_category_digest(date_str: str, category: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO digest_cache (date, category, digest_text) VALUES (?, ?, ?)
        ON CONFLICT(date, category) DO UPDATE SET digest_text = excluded.digest_text
    """, (date_str, category, text))
    conn.commit()
    conn.close()


def get_all_cached_categories(date_str: str) -> dict[str, str]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT category, digest_text FROM digest_cache WHERE date = ?", (date_str,)).fetchall()
    conn.close()
    return {cat: text for cat, text in rows}


def has_today_digest() -> bool:
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT COUNT(*) FROM digest_cache WHERE date = ?", (today,)).fetchone()
    conn.close()
    return row[0] > 0


def cleanup_old_cache():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM digest_cache WHERE date < date('now', '-7 days')")
    conn.commit()
    conn.close()


# ─── RSS-парсер ──────────────────────────────────────────────────────────────


def fetch_news_by_category() -> dict[str, str]:
    news_by_cat: dict[str, list[str]] = {k: [] for k in CATEGORIES}

    for feed_name, feed_url, category in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:4]:
                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link = entry.get("link", "")
                published = entry.get("published", "")

                summary = re.sub(r"<[^>]+>", "", summary).strip()
                if len(summary) > 400:
                    summary = summary[:400] + "..."

                news_by_cat[category].append(
                    f"[{feed_name}]\n"
                    f"Título: {title}\n"
                    f"Resumen: {summary}\n"
                    f"Enlace: {link}\n"
                    f"Fecha: {published}"
                )
        except Exception as e:
            logger.warning(f"Ошибка RSS {feed_name}: {e}")

    return {cat: "\n\n---\n\n".join(entries) for cat, entries in news_by_cat.items() if entries}


# ─── Claude API ──────────────────────────────────────────────────────────────

CATEGORY_PROMPTS = {
    "politics": """Раздел: 1️⃣ *ПОЛИТИКА*
Подтемы: национальная политика Испании, взаимоотношения с ЕС и миром, решения правительства, выборы, реформы.
Стиль: серьёзный, без юмора. 2-4 главных новости, каждая 1-2 предложения.""",

    "economy": """Раздел: 2️⃣ *ЭКОНОМИКА И БИЗНЕС*
Подтемы: инфляция, курс валют, рынок труда, налоги, туризм, недвижимость, стартапы.
Стиль: деловой, но понятный. 2-3 новости, каждая 1-2 предложения.""",

    "society": """Раздел: 3️⃣ *ОБЩЕСТВО*
Подтемы: здравоохранение, образование, законы, соцпрограммы, миграция.
Стиль: информативный, с заботой. 1-2 новости, каждая 1-2 предложения.""",

    "local": """Раздел: 4️⃣ *БАРСЕЛОНА / МАДРИД*
Подтемы: главное из Барселоны и Мадрида — пробки, инфраструктура, городские события.
Стиль: живой, местный колорит. 2-3 новости, каждая 1-2 предложения.""",

    "incidents": """Раздел: 5️⃣ *ПРОИСШЕСТВИЯ*
Подтемы: ДТП, криминал, ЧП, экстренные события, погодные предупреждения.
Стиль: строгий, фактический, БЕЗ юмора. 1-2 новости, каждая 1-2 предложения.""",

    "sports": """Раздел: 6️⃣ *СПОРТ*
Подтемы: футбол (Ла Лига, сборная), теннис, баскетбол, Формула-1, результаты, трансферы.
Стиль: энергичный, можно с юмором. 2-3 новости, каждая 1-2 предложения.""",

    "culture": """Раздел: 7️⃣ *КУЛЬТУРА И АФИША*
Подтемы: выставки, музеи, фестивали, театр, кино, концерты, афиша на выходные.
Стиль: вдохновляющий, с рекомендациями. 2-3 новости, каждая 1-2 предложения.""",

    "celebrities": """Раздел: 8️⃣ *ЗНАМЕНИТОСТИ*
Подтемы: испанские звёзды, светская хроника, королевская семья, мемы и тренды.
Стиль: лёгкий, развлекательный. 1-2 новости, каждая 1-2 предложения.""",

    "humor": """Раздел: 9️⃣ *ЮМОР И КУРЬЁЗЫ*
Подтемы: забавные заголовки, случаи из соцсетей, курьёзные новости.
Стиль: весёлый, с шутками! Это самый лёгкий раздел. 1-2 новости, каждая 1-2 предложения.""",
}

BASE_SYSTEM = """Ты — редактор русскоязычного дайджеста «Испания Daily» 🇪🇸
Аудитория — русскоязычные жители Испании.

ПРАВИЛА:
- Лаконично! Не лей воду.
- Свежий, живой, но ГРАМОТНЫЙ русский язык. Никакого сленга, жаргона, панибратства.
- Тон: профессиональный новостной дайджест с лёгкой подачей. Как хороший журналист, НЕ как блогер.
- НЕ используй: «земель», «братан», «чел», «кринж», «вайб» и подобный сленг.
- НЕ обращайся к читателю на «ты». Пиши безлично или на «вы».
- Поясняй испанские реалии: «Moncloa» → «Монклоа (резиденция премьера)»
- В конце каждой новости — ссылка: [→ источник](url)
- НЕ придумывай новости. Только из предоставленных материалов.
- Формат: Telegram Markdown (*жирный*, _курсив_)
- Если нет достойных новостей — напиши одной строкой: «Сегодня без значимых новостей в этом разделе.»
"""


async def generate_category_digest(category: str, news_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now(TIMEZONE).strftime("%d %B %Y")
    cat_prompt = CATEGORY_PROMPTS.get(category, "")

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1000,
        system=BASE_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Дата: {today}\n\n{cat_prompt}\n\n"
                f"Новости на испанском:\n\n{news_text}\n\n"
                "Создай раздел дайджеста на русском. Только этот раздел, без заголовка дня."
            ),
        }],
    )
    return message.content[0].text


async def generate_all_digests():
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    if has_today_digest():
        logger.info(f"📦 Дайджест за {today} уже есть")
        return

    logger.info(f"🔄 Генерирую дайджесты за {today}...")
    news_by_cat = fetch_news_by_category()

    for category in CATEGORIES:
        news = news_by_cat.get(category, "")
        if not news:
            text = f"{CATEGORIES[category]['emoji']} *{CATEGORIES[category]['name']}*\nСегодня без значимых новостей."
        else:
            try:
                text = await generate_category_digest(category, news)
            except Exception as e:
                logger.error(f"Ошибка генерации {category}: {e}")
                text = f"{CATEGORIES[category]['emoji']} *{CATEGORIES[category]['name']}*\nНе удалось загрузить."

        save_category_digest(today, category, text)
        logger.info(f"  ✅ {category}")

    cleanup_old_cache()
    logger.info(f"🎉 Все 9 категорий за {today} готовы!")


def build_personal_digest(subs: list[str]) -> str:
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    today_display = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    cached = get_all_cached_categories(today)

    if not cached:
        return "😔 Дайджест ещё не готов. Попробуй чуть позже!"

    parts = [f"☀️ *Испания Daily* — {today_display}\n"]
    for cat_key in ALL_CATEGORY_KEYS:
        if cat_key in subs and cat_key in cached:
            parts.append(cached[cat_key])
            parts.append("")

    parts.append("Хорошего дня! 🇪🇸")
    return "\n".join(parts)


def build_full_digest() -> str:
    """Полный дайджест со всеми 9 категориями (для /start)."""
    return build_personal_digest(ALL_CATEGORY_KEYS)


# ─── Отправка ────────────────────────────────────────────────────────────────


async def send_digest_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        if len(text) > 4096:
            parts = [text[i : i + 4096] for i in range(0, len(text), 4096)]
            for part in parts:
                await context.bot.send_message(
                    chat_id=chat_id, text=part, parse_mode="Markdown", disable_web_page_preview=True
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown", disable_web_page_preview=True
            )
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки → {chat_id}: {e}")
        return False


# ─── Закреплённое меню ───────────────────────────────────────────────────────

MENU_TEXT = (
    "📌 *Меню Испания Daily*\n\n"
    "📰 /topics — Выбрать темы\n"
    "⏰ /time — Изменить время\n"
    "⏸ /stop — Пауза\n"
    "▶️ /resume — Возобновить\n"
    "📖 /help — Справка"
)


async def send_and_pin_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправить меню и попробовать закрепить."""
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=MENU_TEXT,
        parse_mode="Markdown",
    )
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception as e:
        logger.warning(f"Не удалось закрепить меню для {chat_id}: {e}")


# ─── Telegram-команды ────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Поток /start:
    1. Приветствие
    2. Полный дайджест (все 9 категорий)
    3. Выбор тем
    4. Выбор времени
    5. Закреплённое меню
    """
    user = update.effective_user
    upsert_user(user.id, user.username or user.first_name)
    chat_id = update.effective_chat.id

    # 1. Приветствие
    if has_today_digest():
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n"
            "Я — *Испания Daily* 🇪🇸\n\n"
            "Вот все новости Испании за сегодня:",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n"
            "Я — *Испания Daily* 🇪🇸\n\n"
            "⏳ Собираю сегодняшние новости... 1-2 минуты.",
            parse_mode="Markdown",
        )
        await generate_all_digests()

    # 2. Полный дайджест (все 9 категорий)
    full_digest = build_full_digest()
    await send_digest_message(chat_id, full_digest, context)

    # 3. Выбор тем
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "👆 Это был полный дайджест из 9 разделов.\n\n"
            "📰 *Теперь выбери, какие темы получать ежедневно:*\n"
            "Нажми на тему чтобы включить/выключить.\n"
            "По умолчанию — все включены."
        ),
        parse_mode="Markdown",
        reply_markup=build_topics_keyboard(user.id),
    )

    # Сохраняем в context что пользователь в процессе онбординга
    context.user_data["onboarding"] = True


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📰 *Темы дайджеста*\n"
        "✅ = подписан  |  ❌ = выключено",
        parse_mode="Markdown",
        reply_markup=build_topics_keyboard(update.effective_user.id),
    )


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hour, minute = get_user_time(update.effective_user.id)
    await update.message.reply_text(
        f"⏰ Сейчас: *{hour:02d}:{minute:02d}* (Мадрид)\nВыбери новое время 👇",
        parse_mode="Markdown",
        reply_markup=build_time_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команды бота:*\n\n"
        "/start — Запустить + получить дайджест\n"
        "/topics — Выбрать темы подписки\n"
        "/time — Изменить время дайджеста\n"
        "/stop — Приостановить рассылку\n"
        "/resume — Возобновить рассылку\n"
        "/help — Эта справка\n\n"
        "📰 *9 разделов:*\n"
        "Политика • Экономика • Общество • Регионы\n"
        "Происшествия • Спорт • Культура • Знаменитости • Юмор",
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика — только для админа."""
    if update.effective_user.id != ADMIN_ID:
        return

    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
    paused = total - active

    # Популярные часы
    time_rows = conn.execute(
        "SELECT digest_hour, COUNT(*) as cnt FROM users WHERE is_active = 1 GROUP BY digest_hour ORDER BY cnt DESC LIMIT 5"
    ).fetchall()
    top_times = "\n".join(f"  {h:02d}:00 — {c} чел." for h, c in time_rows) if time_rows else "  нет данных"

    # Подписки по категориям
    sub_rows = conn.execute("SELECT subscriptions FROM users WHERE is_active = 1").fetchall()
    conn.close()

    cat_count = {k: 0 for k in CATEGORIES}
    for row in sub_rows:
        try:
            subs = json.loads(row[0]) if row[0] else []
            for s in subs:
                if s in cat_count:
                    cat_count[s] += 1
        except json.JSONDecodeError:
            pass

    top_cats = "\n".join(
        f"  {CATEGORIES[k]['emoji']} {CATEGORIES[k]['short']}: {v}"
        for k, v in sorted(cat_count.items(), key=lambda x: -x[1])
    )

    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    cache_ready = "✅ готов" if has_today_digest() else "❌ не готов"

    await update.message.reply_text(
        f"📊 *Статистика Испания Daily*\n\n"
        f"👥 Всего пользователей: *{total}*\n"
        f"✅ Активных: *{active}*\n"
        f"⏸ На паузе: *{paused}*\n\n"
        f"📰 *Подписки по темам:*\n{top_cats}\n\n"
        f"⏰ *Популярные часы:*\n{top_times}\n\n"
        f"🗓 Дайджест за {today}: {cache_ready}",
        parse_mode="Markdown",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_active = 0 WHERE chat_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("⏸ Рассылка приостановлена.\n/resume — возобновить.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_active = 1 WHERE chat_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    hour, minute = get_user_time(update.effective_user.id)
    await update.message.reply_text(
        f"▶️ Возобновлено! Дайджест в *{hour:02d}:{minute:02d}*",
        parse_mode="Markdown",
    )


# ─── Клавиатуры ──────────────────────────────────────────────────────────────


def build_topics_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    subs = get_user_subs(chat_id)
    keyboard = []
    for key, info in CATEGORIES.items():
        status = "✅" if key in subs else "❌"
        keyboard.append([InlineKeyboardButton(
            f"{status} {info['emoji']} {info['name']}",
            callback_data=f"topic_{key}"
        )])
    keyboard.append([
        InlineKeyboardButton("✅ Все", callback_data="topic_all"),
        InlineKeyboardButton("❌ Сброс", callback_data="topic_none"),
    ])
    keyboard.append([InlineKeyboardButton("👌 Готово", callback_data="topic_done")])
    return InlineKeyboardMarkup(keyboard)


def build_time_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌅 07:00", callback_data="time_07_00"),
            InlineKeyboardButton("☀️ 08:00", callback_data="time_08_00"),
            InlineKeyboardButton("🌤 09:00", callback_data="time_09_00"),
        ],
        [
            InlineKeyboardButton("🕙 10:00", callback_data="time_10_00"),
            InlineKeyboardButton("🕛 12:00", callback_data="time_12_00"),
            InlineKeyboardButton("🕐 13:00", callback_data="time_13_00"),
        ],
        [
            InlineKeyboardButton("🌇 18:00", callback_data="time_18_00"),
            InlineKeyboardButton("🌆 19:00", callback_data="time_19_00"),
            InlineKeyboardButton("🌙 21:00", callback_data="time_21_00"),
        ],
    ])


# ─── Обработка кнопок ────────────────────────────────────────────────────────


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.from_user.id
    is_onboarding = context.user_data.get("onboarding", False)

    # ── Время ──
    if data.startswith("time_"):
        parts = data.split("_")
        hour, minute = int(parts[1]), int(parts[2])
        set_user_time(chat_id, hour, minute)

        if is_onboarding:
            # Онбординг завершён → закрепляем меню
            context.user_data["onboarding"] = False
            set_onboarding_done(chat_id)

            subs = get_user_subs(chat_id)
            count = len(subs)

            await query.edit_message_text(
                f"🎉 *Всё готово!*\n\n"
                f"📰 Подписка: {count} из 9 тем\n"
                f"⏰ Время: *{hour:02d}:{minute:02d}* (Мадрид)\n\n"
                "Завтра в это время получишь свой первый автоматический дайджест!",
                parse_mode="Markdown",
            )

            # Закрепляем меню
            await send_and_pin_menu(chat_id, context)
        else:
            await query.edit_message_text(
                f"✅ Дайджест каждый день в *{hour:02d}:{minute:02d}* (Мадрид)",
                parse_mode="Markdown",
            )

    # ── Темы ──
    elif data.startswith("topic_"):
        action = data.replace("topic_", "")

        if action == "done":
            subs = get_user_subs(chat_id)
            count = len(subs)
            names = ", ".join(CATEGORIES[s]["short"] for s in ALL_CATEGORY_KEYS if s in subs)

            if is_onboarding:
                # Переходим к выбору времени
                await query.edit_message_text(
                    f"✅ Выбрано тем: {count} из 9\n"
                    f"📰 {names}\n\n"
                    "⏰ *Теперь выбери время дайджеста* (по Мадриду):",
                    parse_mode="Markdown",
                    reply_markup=build_time_keyboard(),
                )
            else:
                await query.edit_message_text(
                    f"✅ Подписка обновлена! ({count} из 9)\n📰 {names}",
                    parse_mode="Markdown",
                )

        elif action == "all":
            set_user_subs(chat_id, ALL_CATEGORY_KEYS.copy())
            await query.edit_message_text(
                "📰 *Темы дайджеста*\n✅ = подписан  |  ❌ = выключено",
                parse_mode="Markdown",
                reply_markup=build_topics_keyboard(chat_id),
            )
        elif action == "none":
            set_user_subs(chat_id, [])
            await query.edit_message_text(
                "📰 *Темы дайджеста*\n✅ = подписан  |  ❌ = выключено",
                parse_mode="Markdown",
                reply_markup=build_topics_keyboard(chat_id),
            )
        else:
            toggle_user_sub(chat_id, action)
            await query.edit_message_text(
                "📰 *Темы дайджеста*\n✅ = подписан  |  ❌ = выключено",
                parse_mode="Markdown",
                reply_markup=build_topics_keyboard(chat_id),
            )


# ─── Планировщик ─────────────────────────────────────────────────────────────


async def job_generate(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    if now.hour == DIGEST_GEN_HOUR and now.minute == 0:
        await generate_all_digests()


async def job_send(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    users = get_users_for_hour(now.hour, now.minute)
    if not users:
        return

    logger.info(f"⏰ {now.strftime('%H:%M')} — рассылка {len(users)} пользователям")
    for chat_id, subs in users:
        if subs:
            digest = build_personal_digest(subs)
            await send_digest_message(chat_id, digest, context)


# ─── Запуск ──────────────────────────────────────────────────────────────────


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CallbackQueryHandler(button_callback))

    job_queue = app.job_queue
    job_queue.run_repeating(job_generate, interval=60, first=10)
    job_queue.run_repeating(job_send, interval=60, first=15)

    logger.info("🚀 Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
