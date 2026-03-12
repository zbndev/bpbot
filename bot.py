import os
import aiosqlite
import re
import logging
import csv
import io
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

# --- ЛОГИРОВАНИЕ ---
# Улучшено логирование: только важные сообщения
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.WARNING
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

load_dotenv()
MSK_TZ = pytz.timezone("Europe/Moscow")
DB_NAME = "bp_tracker.db"

# --- БД (aiosqlite) ---
# Использование асинхронной библиотеки aiosqlite для предотвращения блокировок
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS records
                          (chat_id INTEGER, timestamp DATETIME, measurement TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS schedule
                          (chat_id INTEGER PRIMARY KEY, morning TEXT, day TEXT, evening TEXT)""")
        await db.commit()

DEFAULT_MORNING = "08:00"
DEFAULT_DAY = "14:00"
DEFAULT_EVENING = "20:00"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def schedule_user_jobs(chat_id: int, context: ContextTypes.DEFAULT_TYPE | Application):
    """Настройка напоминаний для конкретного пользователя"""
    # Очистка старых задач
    job_queue = context.job_queue if hasattr(context, 'job_queue') else context.job_queue
    for period in ["morning", "day", "evening"]:
        for job in job_queue.get_jobs_by_name(f"{chat_id}_{period}"):
            job.schedule_removal()

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT morning, day, evening FROM schedule WHERE chat_id=?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()

    if row:
        times = {"morning": row[0], "day": row[1], "evening": row[2]}
        for period, t_str in times.items():
            if t_str != "OFF":
                try:
                    t_obj = datetime.strptime(t_str, "%H:%M").time().replace(tzinfo=MSK_TZ)
                    job_queue.run_daily(
                        send_reminder,
                        t_obj,
                        chat_id=chat_id,
                        name=f"{chat_id}_{period}",
                        data=period,
                    )
                    logger.info(f"Scheduled {period} job for user {chat_id} at {t_str} MSK")
                except Exception as e:
                    logger.error(f"Error scheduling job for {chat_id}: {e}")

# --- ОБРАБОТЧИКИ КОМАНД ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    logger.info(f"User {chat_id} started the bot")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO schedule (chat_id, morning, day, evening) VALUES (?, ?, ?, ?)",
            (chat_id, DEFAULT_MORNING, DEFAULT_DAY, DEFAULT_EVENING),
        )
        await db.commit()
    
    await schedule_user_jobs(chat_id, context)

    welcome_text = (
        "Привет! Я готов записывать ваши показания.\n\n"
        "📝 <b>Как записать:</b>\n"
        "Просто отправьте сообщение: <code>120/80</code>, <code>120 80</code> или <code>120/80 85</code>.\n\n"
        "📊 <b>Статистика:</b>\n"
        "/stats_3 — за последние 3 дня записей\n"
        "/stats_7 — за последние 7 дней записей\n"
        "/export — скачать всю историю (CSV)\n\n"
        "⚙️ <b>Настройки:</b>\n"
        "/settings — настроить время напоминаний\n"
        "/delete_last — удалить последнюю запись"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    period_names = {"morning": "утреннего", "day": "дневного", "evening": "вечернего"}
    period = context.job.data
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"⏰ Время для {period_names.get(period, '')} измерения давления и пульса!",
    )

# --- НАСТРОЙКИ ЧЕРЕЗ КНОПКИ ---
# Улучшение UX: замена множества команд на одно меню настроек
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🌅 Утро", callback_data="set_morning"),
         InlineKeyboardButton("☀️ День", callback_data="set_day"),
         InlineKeyboardButton("🌙 Вечер", callback_data="set_evening")],
        [InlineKeyboardButton("❌ Отключить утро", callback_data="off_morning")],
        [InlineKeyboardButton("❌ Отключить день", callback_data="off_day")],
        [InlineKeyboardButton("❌ Отключить вечер", callback_data="off_evening")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите, что настроить (Время Московское!):", reply_markup=reply_markup)

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data.startswith("set_"):
        period = data.split("_")[1]
        context.user_data["waiting_for_time"] = period
        await query.edit_message_text(f"Введите время для {period} в формате ЧЧ:ММ (например, 08:30):")
    elif data.startswith("off_"):
        period = data.split("_")[1]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(f"UPDATE schedule SET {period}=? WHERE chat_id=?", ("OFF", chat_id))
            await db.commit()
        await schedule_user_jobs(chat_id, context)
        await query.edit_message_text(f"✅ Напоминание ({period}) отключено.")

# --- ЛОГИКА ЗАПИСИ ---

async def log_measurement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка, не ждем ли мы ввод времени для настроек
    if "waiting_for_time" in context.user_data:
        period = context.user_data.pop("waiting_for_time")
        time_str = update.message.text.strip()
        try:
            datetime.strptime(time_str, "%H:%M")
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(f"UPDATE schedule SET {period}=? WHERE chat_id=?", (time_str, update.message.chat_id))
                await db.commit()
            await schedule_user_jobs(update.message.chat_id, context)
            await update.message.reply_text(f"✅ Время для {period} установлено на {time_str}.")
            return
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Попробуйте еще раз (ЧЧ:ММ) или введите /start для отмены.")
            context.user_data["waiting_for_time"] = period
            return

    # Улучшенный Regex: теперь понимает 120/80, 120 80, 120-80 и т.д.
    text = update.message.text.strip()
    match = re.match(r"^(\d{2,3})[\s/-]+(\d{2,3})(?:[\s/-]+(\d{2,3}))?$", text)
    
    if not match:
        await update.message.reply_text(
            "⚠️ Неверный формат. Используйте: <code>120/80</code> или <code>120/80 85</code>.",
            parse_mode="HTML",
        )
        return

    # Форматируем для единообразия в БД: "САД/ДАД Пульс"
    sys, dia, pulse = match.groups()
    clean_text = f"{sys}/{dia}" + (f" {pulse}" if pulse else "")
    
    chat_id = update.message.chat_id
    now_msk = datetime.now(MSK_TZ)
    timestamp = now_msk.strftime("%Y-%m-%d %H:%M")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO records VALUES (?, ?, ?)", (chat_id, timestamp, clean_text))
        # Автоматическая очистка: удаление записей старше 14 дней (увеличено с 7)
        two_weeks_ago = (now_msk - timedelta(days=14)).strftime("%Y-%m-%d %H:%M")
        await db.execute("DELETE FROM records WHERE chat_id=? AND timestamp < ?", (chat_id, two_weeks_ago))
        await db.commit()

    await update.message.reply_text(
        f"✅ Записано: {clean_text} (Время: {timestamp[-5:]} МСК)", parse_mode="HTML"
    )

# --- СТАТИСТИКА И ЭКСПОРТ ---

async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int):
    chat_id = update.message.chat_id
    
    async with aiosqlite.connect(DB_NAME) as db:
        # Логика сохранена: отсчет от последней записи пользователя
        async with db.execute(
            "SELECT timestamp FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 1", (chat_id,)
        ) as cursor:
            latest_record = await cursor.fetchone()

        if not latest_record:
            await update.message.reply_text("У вас пока нет записей.")
            return

        latest_date = datetime.strptime(latest_record[0], "%Y-%m-%d %H:%M")
        start_date = (latest_date - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00")

        async with db.execute(
            "SELECT timestamp, measurement FROM records WHERE chat_id=? AND timestamp >= ? ORDER BY timestamp ASC",
            (chat_id, start_date),
        ) as cursor:
            records = await cursor.fetchall()

    if not records:
        await update.message.reply_text(f"За этот период записей не найдено.")
        return

    response = f"📊 <b>Статистика (за {days} дн. с последней записи):</b>\n\n"
    for row in records:
        date_obj = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
        response += f"🔹 {date_obj.strftime('%d.%m %H:%M')} — {row[1]}\n"

    await update.message.reply_text(response, parse_mode="HTML")

async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление самой последней записи пользователя"""
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        # Сначала найдем, что удаляем, для уведомления
        async with db.execute(
            "SELECT timestamp, measurement FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 1", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        
        if row:
            await db.execute(
                "DELETE FROM records WHERE rowid = (SELECT rowid FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 1)",
                (chat_id,)
            )
            await db.commit()
            await update.message.reply_text(f"🗑 Удалена запись: {row[1]} ({row[0]})")
        else:
            await update.message.reply_text("У вас нет записей для удаления.")

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт всех данных пользователя в CSV"""
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT timestamp, measurement FROM records WHERE chat_id=? ORDER BY timestamp ASC", (chat_id,)
        ) as cursor:
            records = await cursor.fetchall()

    if not records:
        await update.message.reply_text("Нет данных для экспорта.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Дата и время", "Показания (САД/ДАД Пульс)"])
    writer.writerows(records)
    
    output.seek(0)
    # Отправляем файл пользователю
    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(output.getvalue().encode('utf-8')),
        filename=f"bp_history_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="Ваша история измерений 📊"
    )

# --- ИНИЦИАЛИЗАЦИЯ ПРИ ЗАПУСКЕ ---

async def post_init(application: Application):
    await init_db()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id FROM schedule") as cursor:
            users = await cursor.fetchall()
    for (chat_id,) in users:
        await schedule_user_jobs(chat_id, application)

if __name__ == "__main__":
    TOKEN = os.getenv("TG_TOKEN")
    if not TOKEN:
        raise ValueError("TG_TOKEN не найден в .env")

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Регистрация команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CommandHandler("stats_3", lambda u, c: get_stats(u, c, 3)))
    application.add_handler(CommandHandler("stats_7", lambda u, c: get_stats(u, c, 7)))
    application.add_handler(CommandHandler("delete_last", delete_last))
    application.add_handler(CommandHandler("export", export_data))
    
    # Регистрация обработчиков настроек
    application.add_handler(CallbackQueryHandler(settings_callback))
    
    # Обработка текста (измерения или ввод времени)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_measurement))

    logger.info("Бот запущен...")
    application.run_polling()
