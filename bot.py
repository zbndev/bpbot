import os
import sqlite3
import re
import logging
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
MSK_TZ = pytz.timezone("Europe/Moscow")

conn = sqlite3.connect("bp_tracker.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""CREATE TABLE IF NOT EXISTS records
                  (chat_id INTEGER, timestamp DATETIME, measurement TEXT)""")
cursor.execute("""CREATE TABLE IF NOT EXISTS schedule
                  (chat_id INTEGER PRIMARY KEY, morning TEXT, day TEXT, evening TEXT)""")
conn.commit()

DEFAULT_MORNING = "08:00"
DEFAULT_DAY = "14:00"
DEFAULT_EVENING = "20:00"


def schedule_user_jobs(chat_id: int, context: ContextTypes.DEFAULT_TYPE | Application):
    for period in ["morning", "day", "evening"]:
        for job in context.job_queue.get_jobs_by_name(f"{chat_id}_{period}"):
            job.schedule_removal()

    cursor.execute(
        "SELECT morning, day, evening FROM schedule WHERE chat_id=?", (chat_id,)
    )
    row = cursor.fetchone()

    if row:
        times = {"morning": row[0], "day": row[1], "evening": row[2]}
        for period, t_str in times.items():
            if t_str != "OFF":
                t_obj = datetime.strptime(t_str, "%H:%M").time().replace(tzinfo=MSK_TZ)
                context.job_queue.run_daily(
                    send_reminder,
                    t_obj,
                    chat_id=chat_id,
                    name=f"{chat_id}_{period}",
                    data=period,
                )
                logger.info(f"Scheduled {period} job for user {chat_id} at {t_str} MSK")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    cursor.execute(
        "INSERT OR IGNORE INTO schedule (chat_id, morning, day, evening) VALUES (?, ?, ?, ?)",
        (chat_id, DEFAULT_MORNING, DEFAULT_DAY, DEFAULT_EVENING),
    )
    conn.commit()
    schedule_user_jobs(chat_id, context)

    welcome_text = (
        "Привет! Я готов записывать ваши показания.\n\n"
        "📝 <b>Как записать:</b>\n"
        "Просто отправьте сообщение в формате <code>120/80</code> или <code>120/80 85</code> (если с пульсом).\n\n"
        "📊 <b>Статистика:</b>\n"
        "/stats_3 — показать записи за последние 3 дня\n"
        "/stats_7 — показать записи за последние 7 дней\n\n"
        "⏰ <b>Настройка напоминаний (Время Московское!):</b>\n"
        "Используйте команды с указанием времени (в формате ЧЧ:ММ):\n"
        "/set_morning 08:30 — задать утреннее измерение\n"
        "/set_day 14:00 — задать дневное измерение\n"
        "/set_evening 21:00 — задать вечернее измерение\n\n"
        "🔇 <b>Отключить напоминания:</b>\n"
        "/set_morning_off — отключить утреннее\n"
        "/set_day_off — отключить дневное\n"
        "/set_evening_off — отключить вечернее"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    period_names = {"morning": "утреннего", "day": "дневного", "evening": "вечернего"}
    period = context.job.data
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"⏰ Время для {period_names.get(period, '')} измерения давления и пульса!",
    )


async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    if not context.args:
        await update.message.reply_text(
            f"Пожалуйста, укажите время. Пример: /set_{period} 08:30"
        )
        return

    time_str = context.args[0]
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "Неверный формат времени. Используйте ЧЧ:ММ (например, 08:30)."
        )
        return

    chat_id = update.message.chat_id
    cursor.execute(
        f"UPDATE schedule SET {period}=? WHERE chat_id=?", (time_str, chat_id)
    )
    conn.commit()
    schedule_user_jobs(chat_id, context)

    period_ru = {"morning": "Утреннее", "day": "Дневное", "evening": "Вечернее"}[period]
    await update.message.reply_text(
        f"✅ {period_ru} напоминание успешно установлено на {time_str} (МСК)."
    )


async def disable_time(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    chat_id = update.message.chat_id
    cursor.execute(f"UPDATE schedule SET {period}=? WHERE chat_id=?", ("OFF", chat_id))
    conn.commit()
    schedule_user_jobs(chat_id, context)

    period_ru = {"morning": "Утреннее", "day": "Дневное", "evening": "Вечернее"}[period]
    await update.message.reply_text(f"🔇 {period_ru} напоминание отключено.")


async def cmd_set_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_time(update, context, "morning")


async def cmd_set_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_time(update, context, "day")


async def cmd_set_evening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_time(update, context, "evening")


async def cmd_set_morning_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await disable_time(update, context, "morning")


async def cmd_set_day_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await disable_time(update, context, "day")


async def cmd_set_evening_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await disable_time(update, context, "evening")


async def log_measurement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not re.match(r"^\d{2,3}\s*/\s*\d{2,3}(?:\s+\d{2,3})?$", text):
        await update.message.reply_text(
            "⚠️ Неверный формат. Пожалуйста, используйте формат <code>120/80</code> или <code>120/80 85</code>.",
            parse_mode="HTML",
        )
        return

    chat_id = update.message.chat_id
    now_msk = datetime.now(MSK_TZ)
    timestamp = now_msk.strftime("%Y-%m-%d %H:%M")

    cursor.execute("INSERT INTO records VALUES (?, ?, ?)", (chat_id, timestamp, text))

    # Auto-rotation: Delete user's records older than 7 days
    seven_days_ago = (now_msk - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    cursor.execute(
        "DELETE FROM records WHERE chat_id=? AND timestamp < ?",
        (chat_id, seven_days_ago),
    )
    conn.commit()

    await update.message.reply_text(
        f"✅ Записано: {text} (Время: {timestamp[-5:]} МСК)", parse_mode="HTML"
    )


async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int):
    chat_id = update.message.chat_id
    now_msk = datetime.now(MSK_TZ)
    start_date = (now_msk - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00")

    cursor.execute(
        "SELECT timestamp, measurement FROM records WHERE chat_id=? AND timestamp >= ? ORDER BY timestamp ASC",
        (chat_id, start_date),
    )
    records = cursor.fetchall()

    if not records:
        await update.message.reply_text(f"За последние {days} дней записей не найдено.")
        return

    response = f"📊 <b>Ваша статистика за {days} дней:</b>\n\n"
    for row in records:
        date_obj = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
        response += f"🔹 {date_obj.strftime('%d.%m %H:%M')} — {row[1]}\n"

    await update.message.reply_text(response, parse_mode="HTML")


async def cmd_stats_3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_stats(update, context, 3)


async def cmd_stats_7(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_stats(update, context, 7)


async def post_init(application: Application):
    cursor.execute("SELECT chat_id FROM schedule")
    users = cursor.fetchall()
    for (chat_id,) in users:
        schedule_user_jobs(chat_id, application)


if __name__ == "__main__":
    TOKEN = os.getenv("TG_TOKEN")
    if not TOKEN:
        logger.error(
            "Критическая ошибка: Токен не найден. Убедитесь, что файл .env создан и содержит TG_TOKEN."
        )
        raise ValueError("Токен не найден.")

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats_3", cmd_stats_3))
    application.add_handler(CommandHandler("stats_7", cmd_stats_7))

    application.add_handler(CommandHandler("set_morning", cmd_set_morning))
    application.add_handler(CommandHandler("set_day", cmd_set_day))
    application.add_handler(CommandHandler("set_evening", cmd_set_evening))

    application.add_handler(CommandHandler("set_morning_off", cmd_set_morning_off))
    application.add_handler(CommandHandler("set_day_off", cmd_set_day_off))
    application.add_handler(CommandHandler("set_evening_off", cmd_set_evening_off))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, log_measurement)
    )

    logger.info("Бот запущен...")
    application.run_polling()
