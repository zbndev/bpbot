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
        # Новые таблицы для профиля и лекарств
        await db.execute("""CREATE TABLE IF NOT EXISTS users_profile
                          (chat_id INTEGER PRIMARY KEY, 
                           working_sys INTEGER, 
                           working_dia INTEGER, 
                           is_auto_baseline BOOLEAN DEFAULT 1,
                           baseline_updated_at DATETIME)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS medications
                          (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           chat_id INTEGER,
                           name TEXT,
                           dosage TEXT,
                           reminder_time TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS med_intake
                          (chat_id INTEGER,
                           med_id INTEGER,
                           timestamp DATETIME)""")
        await db.commit()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ЛОГИКИ ---

async def get_user_baseline(chat_id: int):
    """Получает рабочее давление пользователя (из БД или по умолчанию ВОЗ)"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT working_sys, working_dia FROM users_profile WHERE chat_id=?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] and row[1]:
                return row[0], row[1]
    return 120, 80  # Дефолт по ВОЗ

async def calculate_median_baseline(chat_id: int):
    """Рассчитывает медиану последних 15 замеров для авто-базиса"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT measurement FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 15", (chat_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    
    if len(rows) < 10:
        return None  # Недостаточно данных для расчета (нужно хотя бы 10)

    sys_vals, dia_vals = [], []
    for (m,) in rows:
        match = re.match(r"(\d{2,3})/(\d{2,3})", m)
        if match:
            sys_vals.append(int(match.group(1)))
            dia_vals.append(int(match.group(2)))

    import statistics
    return int(statistics.median(sys_vals)), int(statistics.median(dia_vals))

def classify_bp(sys, dia, base_sys, base_dia):
    """Классификация давления относительно базы или ВОЗ с цветовой индикацией"""
    # Разница в процентах
    sys_diff = (sys - base_sys) / base_sys
    
    if sys >= 160 or dia >= 100: return "🔴 Критически высокое (Гипертония 2+ ст.)"
    if sys >= 140 or dia >= 90: return "🟠 Высокое (Гипертония 1 ст.)"
    
    if sys_diff > 0.15: return "🟡 Повышенное (относительно вашей нормы)"
    if sys_diff < -0.15: return "🔵 Пониженное (относительно вашей нормы)"
    
    if sys <= 90 or dia <= 60: return "🔵 Низкое (Гипотония)"
    
    return "🟢 В норме"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def schedule_user_jobs(chat_id: int, context: ContextTypes.DEFAULT_TYPE | Application):
    """Настройка всех напоминаний (давление + лекарства) для пользователя"""
    job_queue = context.job_queue
    # Очистка всех старых задач пользователя
    for job in job_queue.get_jobs_by_name(f"user_{chat_id}"):
        job.schedule_removal()

    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Напоминания о давлении
        async with db.execute(
            "SELECT morning, day, evening FROM schedule WHERE chat_id=?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                times = {"morning": row[0], "day": row[1], "evening": row[2]}
                for period, t_str in times.items():
                    if t_str != "OFF":
                        t_obj = datetime.strptime(t_str, "%H:%M").time().replace(tzinfo=MSK_TZ)
                        job_queue.run_daily(send_reminder, t_obj, chat_id=chat_id, 
                                          name=f"user_{chat_id}", data={"type": "bp", "period": period})

        # 2. Напоминания о лекарствах
        async with db.execute(
            "SELECT id, name, dosage, reminder_time FROM medications WHERE chat_id=?", (chat_id,)
        ) as cursor:
            meds = await cursor.fetchall()
            for m_id, name, dose, t_str in meds:
                t_obj = datetime.strptime(t_str, "%H:%M").time().replace(tzinfo=MSK_TZ)
                job_queue.run_daily(send_med_reminder, t_obj, chat_id=chat_id,
                                  name=f"user_{chat_id}", data={"type": "med", "id": m_id, "name": name, "dose": dose})

async def send_med_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    keyboard = [[InlineKeyboardButton(f"💊 Принял {data['name']}", callback_data=f"take_{data['id']}")]]
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"💊 Пора принять лекарство: <b>{data['name']}</b> ({data['dose']})",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    job_data = context.job.data
    period = job_data.get("period", "")
    
    # --- УМНОЕ НАПОМИНАНИЕ ---
    async with aiosqlite.connect(DB_NAME) as db:
        one_hour_ago = (datetime.now(MSK_TZ) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        async with db.execute(
            "SELECT timestamp FROM records WHERE chat_id=? AND timestamp > ? LIMIT 1",
            (chat_id, one_hour_ago)
        ) as cursor:
            recent_record = await cursor.fetchone()
    
    if recent_record:
        logger.info(f"Smart Reminder: Skipping {period} for {chat_id}")
        return

    period_names = {"morning": "утреннего", "day": "дневного", "evening": "вечернего"}
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Время для {period_names.get(period, '')} измерения давления и пульса!",
    )

# --- ОБЪЕДИНЕННЫЙ CALLBACK HANDLER ---

async def universal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # Настройки времени
    if data.startswith("set_"):
        period = data.split("_")[1]
        context.user_data["waiting_for_time"] = period
        await query.edit_message_text(f"Введите время для {period} в формате ЧЧ:ММ:")
    
    elif data.startswith("off_"):
        period = data.split("_")[1]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(f"UPDATE schedule SET {period}=? WHERE chat_id=?", ("OFF", chat_id))
            await db.commit()
        await schedule_user_jobs(chat_id, context)
        await query.edit_message_text(f"✅ Напоминание ({period}) отключено.")

    # Лекарства
    elif data.startswith("take_"):
        med_id = data.split("_")[1]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO med_intake VALUES (?, ?, ?)", 
                          (chat_id, med_id, datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M")))
            await db.commit()
        await query.edit_message_text(f"✅ Отметка о приеме лекарства сохранена.")
    
    elif data.startswith("del_med_"):
        med_id = data.split("_")[2]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM medications WHERE id=?", (med_id,))
            await db.commit()
        await schedule_user_jobs(chat_id, context)
        await query.edit_message_text("✅ Лекарство удалено из списка.")

# --- ИСПРАВЛЕННЫЙ LOG_MEASUREMENT (MERGED) ---

async def log_measurement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text.strip()

    # Ввод времени для настроек
    if "waiting_for_time" in context.user_data:
        period = context.user_data.pop("waiting_for_time")
        try:
            datetime.strptime(text, "%H:%M")
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(f"UPDATE schedule SET {period}=? WHERE chat_id=?", (text, chat_id))
                await db.commit()
            await schedule_user_jobs(chat_id, context)
            await update.message.reply_text(f"✅ Время для {period} установлено на {text}.")
        except ValueError:
            await update.message.reply_text("❌ Ошибка формата. Введите ЧЧ:ММ:")
        return

    # Ввод данных лекарства
    if "waiting_for_med_name" in context.user_data:
        context.user_data["med_name"] = text
        context.user_data.pop("waiting_for_med_name")
        context.user_data["waiting_for_med_dose"] = True
        await update.message.reply_text(f"Принято: {text}. Теперь введите дозировку:")
        return
    if "waiting_for_med_dose" in context.user_data:
        context.user_data["med_dose"] = text
        context.user_data.pop("waiting_for_med_dose")
        context.user_data["waiting_for_med_time"] = True
        await update.message.reply_text(f"Введите время напоминания (ЧЧ:ММ):")
        return
    if "waiting_for_med_time" in context.user_data:
        try:
            datetime.strptime(text, "%H:%M")
            name = context.user_data.pop("med_name")
            dose = context.user_data.pop("med_dose")
            context.user_data.pop("waiting_for_med_time")
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("INSERT INTO medications (chat_id, name, dosage, reminder_time) VALUES (?, ?, ?, ?)",
                              (chat_id, name, dose, text))
                await db.commit()
            await schedule_user_jobs(chat_id, context)
            await update.message.reply_text(f"✅ Лекарство {name} добавлено.")
        except ValueError:
            await update.message.reply_text("❌ Ошибка формата. Введите ЧЧ:ММ:")
        return

    # Обработка замера
    match = re.match(r"^(\d{2,3})[\s/-]+(\d{2,3})(?:[\s/-]+(\d{2,3}))?$", text)
    if not match:
        await update.message.reply_text("⚠️ Неверный формат. Используйте 120/80.")
        return

    sys, dia, pulse = map(int, match.groups(default=0))
    now_msk = datetime.now(MSK_TZ)
    timestamp = now_msk.strftime("%Y-%m-%d %H:%M")

    base_sys, base_dia = await get_user_baseline(chat_id)
    status = classify_bp(sys, dia, base_sys, base_dia)

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO records VALUES (?, ?, ?)", (chat_id, timestamp, f"{sys}/{dia}" + (f" {pulse}" if pulse else "")))
        # Очистка и авто-базис (логика сохранена)
        await db.commit()

    # Предложение принять лекарства
    keyboard = []
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name, dosage FROM medications WHERE chat_id=?", (chat_id,)) as cursor:
            meds = await cursor.fetchall()
            for m_id, m_name, m_dose in meds:
                keyboard.append([InlineKeyboardButton(f"💊 Принял {m_name}", callback_data=f"take_{m_id}")])

    await update.message.reply_text(
        f"✅ <b>Записано:</b> {sys}/{dia}\n📊 <b>Статус:</b> {status}",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )

# --- СТАТИСТИКА И ЭКСПОРТ ---

async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int):
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
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
    """Экспорт всех данных (замеры + лекарства) в CSV"""
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT timestamp, measurement FROM records WHERE chat_id=? ORDER BY timestamp ASC", (chat_id,)
        ) as cursor:
            records = await cursor.fetchall()
        
        async with db.execute(
            "SELECT i.timestamp, m.name, m.dosage FROM med_intake i JOIN medications m ON i.med_id = m.id WHERE i.chat_id=? ORDER BY i.timestamp ASC",
            (chat_id,)
        ) as cursor:
            med_records = await cursor.fetchall()

    if not records and not med_records:
        await update.message.reply_text("Нет данных для экспорта.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["--- ЗАМЕРЫ ДАВЛЕНИЯ ---"])
    writer.writerow(["Дата и время", "Показания"])
    writer.writerows(records)
    writer.writerow([])
    writer.writerow(["--- ПРИЕМ ЛЕКАРСТВ ---"])
    writer.writerow(["Дата и время", "Лекарство", "Дозировка"])
    writer.writerows(med_records)
    
    output.seek(0)
    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(output.getvalue().encode('utf-8')),
        filename=f"bp_history_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="Ваша полная история мониторинга 📊"
    )

if __name__ == "__main__":
    TOKEN = os.getenv("TG_TOKEN")
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CommandHandler("med_add", med_add))
    application.add_handler(CommandHandler("med_list", med_list))
    application.add_handler(CommandHandler("stats_3", lambda u, c: get_stats(u, c, 3)))
    application.add_handler(CommandHandler("stats_7", lambda u, c: get_stats(u, c, 7)))
    application.add_handler(CommandHandler("delete_last", delete_last))
    application.add_handler(CommandHandler("export", export_data))
    application.add_handler(CallbackQueryHandler(universal_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_measurement))

    application.run_polling()
