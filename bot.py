"""
🇪🇸 Испания Daily — Telegram-бот с ежедневным дайджестом новостей Испании
RSS → Claude API (суммаризация + перевод на русский) → Telegram
"""

import os
import logging
import sqlite3
import asyncio
from datetime import datetime, time
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

# ─── RSS-источники ───────────────────────────────────────────────────────────
# Каждый источник — (название, url, категории)

RSS_FEEDS = [
    # Политика
    ("El País — Política", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/espana/portada", ["политика"]),
    ("El Mundo — España", "https://e00-elmundo.uecdn.es/elmundo/rss/espana.xml", ["политика"]),
    ("20 Minutos — Nacional", "https://www.20minutos.es/rss/nacional/", ["политика"]),

    # Экономика / Недвижимость
    ("El País — Economía", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/economia/portada", ["экономика", "недвижимость"]),
    ("Idealista News", "https://www.idealista.com/news/rss", ["недвижимость"]),

    # Спорт
    ("Marca", "https://e00-marca.uecdn.es/rss/portada.xml", ["спорт"]),
    ("AS", "https://feeds.as.com/mrss-s/pages/as/site/as.com/portada", ["спорт"]),

    # Культура / Концерты / Выставки
    ("El País — Cultura", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/cultura/portada", ["культура", "концерты", "выставки"]),
    ("20 Minutos — Artes", "https://www.20minutos.es/rss/artes/", ["культура", "выставки"]),

    # Общество / Смешное / Русскоязычные
    ("El País — Sociedad", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/sociedad/portada", ["общество", "смешное"]),
    ("20 Minutos — Gente", "https://www.20minutos.es/rss/gente/", ["общество", "смешное"]),
]

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── База данных ─────────────────────────────────────────────────────────────


def init_db():
    """Создаём таблицу пользователей если не существует."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            digest_hour INTEGER DEFAULT 8,
            digest_minute INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def upsert_user(chat_id: int, username: str = ""):
    """Добавить или обновить пользователя."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO users (chat_id, username) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username
        """,
        (chat_id, username),
    )
    conn.commit()
    conn.close()


def set_user_time(chat_id: int, hour: int, minute: int = 0):
    """Установить время дайджеста для пользователя."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE users SET digest_hour = ?, digest_minute = ? WHERE chat_id = ?",
        (hour, minute, chat_id),
    )
    conn.commit()
    conn.close()


def get_users_for_hour(hour: int, minute: int = 0) -> list[int]:
    """Получить chat_id пользователей, которым нужно отправить дайджест сейчас."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT chat_id FROM users WHERE digest_hour = ? AND digest_minute = ? AND is_active = 1",
        (hour, minute),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_user_time(chat_id: int) -> tuple[int, int]:
    """Получить время дайджеста пользователя."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT digest_hour, digest_minute FROM users WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    return row if row else (8, 0)


# ─── RSS-парсер ──────────────────────────────────────────────────────────────


def fetch_all_news() -> str:
    """Собрать новости из всех RSS-лент, вернуть текст для Claude."""
    all_entries = []

    for feed_name, feed_url, categories in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:  # Максимум 5 новостей с каждого источника
                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link = entry.get("link", "")
                published = entry.get("published", "")

                # Очистка HTML-тегов из summary
                import re
                summary = re.sub(r"<[^>]+>", "", summary).strip()
                if len(summary) > 500:
                    summary = summary[:500] + "..."

                all_entries.append(
                    f"[{feed_name}] [{', '.join(categories)}]\n"
                    f"Título: {title}\n"
                    f"Resumen: {summary}\n"
                    f"Enlace: {link}\n"
                    f"Fecha: {published}"
                )
        except Exception as e:
            logger.warning(f"Ошибка при парсинге {feed_name}: {e}")

    return "\n\n---\n\n".join(all_entries)


# ─── Claude API ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — редактор русскоязычного ежедневного дайджеста «Испания Daily» 🇪🇸

Твоя задача: из набора новостей на испанском создать живой, интересный дайджест на русском языке.

СТРУКТУРА ДАЙДЖЕСТА:
1. 🏛 ПОЛИТИКА — главные политические новости (1-2 новости)
2. ⚽ СПОРТ — спортивные события (1-2 новости)
3. 🏠 НЕДВИЖИМОСТЬ — рынок жилья, цены, законы (1 новость если есть)
4. 🎭 КУЛЬТУРА И СОБЫТИЯ — концерты, выставки, фестивали (1-2 новости)
5. 🤝 ДЛЯ НАШИХ — что важно русскоязычным жителям Испании (резиденция, NIE, законы, визы — если есть)
6. 😄 КУРЬЁЗЫ — самое смешное/необычное (1 новость)

ПРАВИЛА:
- Пиши живым, лёгким языком — как будто рассказываешь другу за кофе
- Поясняй испанские реалии: «Moncloa» → «резиденция премьера (Монклоа)»
- Каждая новость — 2-3 предложения максимум
- Добавляй эмодзи для визуального разделения
- В конце каждой новости — ссылка на источник
- Если нет новостей по какой-то категории — пропусти её
- Начинай с приветствия: «☀️ Доброе утро! Вот что происходит в Испании сегодня:»
- Заканчивай: «Хорошего дня! 🌞»
- НЕ придумывай новости, используй ТОЛЬКО предоставленные
- Формат: Telegram Markdown (жирный: *текст*, курсив: _текст_)
"""


async def generate_digest(news_text: str) -> str:
    """Отправить новости в Claude API и получить дайджест."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    today = datetime.now(TIMEZONE).strftime("%d.%m.%Y, %A")

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Сегодня {today}. Вот новости из испанских СМИ за сегодня:\n\n"
                    f"{news_text}\n\n"
                    "Создай дайджест на русском языке по правилам выше."
                ),
            }
        ],
    )

    return message.content[0].text


# ─── Telegram-команды ────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /start — приветствие и выбор времени."""
    user = update.effective_user
    upsert_user(user.id, user.username or user.first_name)

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я — *Испания Daily* 🇪🇸\n\n"
        "Каждый день я собираю самые важные новости Испании из лучших источников, "
        "перевожу и делаю для тебя дайджест на русском.\n\n"
        "📰 *Что внутри:*\n"
        "• Политика и экономика\n"
        "• Спорт\n"
        "• Недвижимость\n"
        "• Концерты и выставки\n"
        "• Полезное для русскоязычных\n"
        "• Курьёзы 😄\n\n"
        "Выбери удобное время для дайджеста 👇",
        parse_mode="Markdown",
    )

    await send_time_picker(update.message.chat_id, context)


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /time — изменить время дайджеста."""
    hour, minute = get_user_time(update.effective_user.id)
    await update.message.reply_text(
        f"⏰ Сейчас дайджест приходит в *{hour:02d}:{minute:02d}* (Мадрид)\n\n"
        "Выбери новое время 👇",
        parse_mode="Markdown",
    )
    await send_time_picker(update.message.chat_id, context)


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /now — получить дайджест прямо сейчас."""
    await update.message.reply_text("⏳ Собираю новости... Это займёт 20-30 секунд.")

    try:
        news_text = fetch_all_news()
        if not news_text:
            await update.message.reply_text("😔 Не удалось собрать новости. Попробуй позже.")
            return

        digest = await generate_digest(news_text)
        # Telegram ограничивает сообщения 4096 символами
        if len(digest) > 4096:
            parts = [digest[i : i + 4096] for i in range(0, len(digest), 4096)]
            for part in parts:
                await update.message.reply_text(part, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await update.message.reply_text(digest, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Ошибка при генерации дайджеста: {e}")
        await update.message.reply_text("😔 Произошла ошибка при создании дайджеста. Попробуй позже.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /help."""
    await update.message.reply_text(
        "📖 *Команды бота:*\n\n"
        "/start — Начать и выбрать время\n"
        "/time — Изменить время дайджеста\n"
        "/now — Получить дайджест прямо сейчас\n"
        "/stop — Приостановить рассылку\n"
        "/resume — Возобновить рассылку\n"
        "/help — Эта справка\n",
        parse_mode="Markdown",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приостановить рассылку."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_active = 0 WHERE chat_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        "⏸ Рассылка приостановлена.\nНапиши /resume чтобы возобновить."
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возобновить рассылку."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_active = 1 WHERE chat_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    hour, minute = get_user_time(update.effective_user.id)
    await update.message.reply_text(
        f"▶️ Рассылка возобновлена!\nДайджест будет приходить в *{hour:02d}:{minute:02d}*",
        parse_mode="Markdown",
    )


# ─── Inline-кнопки выбора времени ────────────────────────────────────────────


async def send_time_picker(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправить клавиатуру с выбором времени."""
    keyboard = [
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
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=chat_id,
        text="⏰ *Выбери время дайджеста* (по Мадриду):",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на inline-кнопки."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("time_"):
        parts = data.split("_")
        hour = int(parts[1])
        minute = int(parts[2])

        set_user_time(query.from_user.id, hour, minute)

        await query.edit_message_text(
            f"✅ Отлично! Дайджест будет приходить каждый день в *{hour:02d}:{minute:02d}* (Мадрид)\n\n"
            "💡 Напиши /now чтобы получить дайджест прямо сейчас!",
            parse_mode="Markdown",
        )


# ─── Планировщик рассылки ────────────────────────────────────────────────────


async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE):
    """Запускается каждую минуту, проверяет кому отправить дайджест."""
    now = datetime.now(TIMEZONE)
    current_hour = now.hour
    current_minute = now.minute

    users = get_users_for_hour(current_hour, current_minute)
    if not users:
        return

    logger.info(f"⏰ {now.strftime('%H:%M')} — отправляю дайджест {len(users)} пользователям")

    # Генерируем дайджест один раз для всех
    try:
        news_text = fetch_all_news()
        if not news_text:
            logger.warning("Нет новостей для дайджеста")
            return

        digest = await generate_digest(news_text)
    except Exception as e:
        logger.error(f"Ошибка генерации дайджеста: {e}")
        return

    # Рассылаем всем пользователям
    for chat_id in users:
        try:
            if len(digest) > 4096:
                parts = [digest[i : i + 4096] for i in range(0, len(digest), 4096)]
                for part in parts:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=part,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=digest,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            logger.info(f"✅ Дайджест отправлен → {chat_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки → {chat_id}: {e}")


# ─── Запуск бота ─────────────────────────────────────────────────────────────


def main():
    """Запуск бота."""
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("resume", cmd_resume))

    # Кнопки
    app.add_handler(CallbackQueryHandler(button_callback))

    # Планировщик — проверяем каждые 60 секунд
    job_queue = app.job_queue
    job_queue.run_repeating(scheduled_digest, interval=60, first=10)

    logger.info("🚀 Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
