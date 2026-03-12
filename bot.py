import os
import aiosqlite
import re
import logging
import csv
import io
import statistics
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
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.WARNING
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

load_dotenv()

# --- КОНСТАНТЫ ---
DEFAULT_MORNING = "08:00"
DEFAULT_DAY = "14:00"
DEFAULT_EVENING = "20:00"
MSK_TZ = pytz.timezone("Europe/Moscow")
DB_NAME = "bp_tracker.db"

# --- БД (aiosqlite) ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS records
                          (chat_id INTEGER, timestamp DATETIME, measurement TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS schedule
                          (chat_id INTEGER PRIMARY KEY, morning TEXT, day TEXT, evening TEXT)""")
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
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT working_sys, working_dia FROM users_profile WHERE chat_id=?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] and row[1]:
                return row[0], row[1]
    return 120, 80

async def calculate_median_baseline(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT measurement FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 15", (chat_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    
    if len(rows) < 10:
        return None

    sys_vals, dia_vals = [], []
    for (m,) in rows:
        match = re.match(r"(\d{2,3})/(\d{2,3})", m)
        if match:
            sys_vals.append(int(match.group(1)))
            dia_vals.append(int(match.group(2)))

    return int(statistics.median(sys_vals)), int(statistics.median(dia_vals))

def classify_bp(sys, dia, base_sys, base_dia):
    sys_diff = (sys - base_sys) / base_sys
    if sys >= 160 or dia >= 100: return "🔴 Критически высокое (Гипертония 2+ ст.)"
    if sys >= 140 or dia >= 90: return "🟠 Высокое (Гипертония 1 ст.)"
    if sys_diff > 0.15: return "🟡 Повышенное (относительно вашей нормы)"
    if sys_diff < -0.15: return "🔵 Пониженное (относительно вашей нормы)"
    if sys <= 90 or dia <= 60: return "🔵 Низкое (Гипотония)"
    return "🟢 В норме"

# --- ПЛАНИРОВЩИК ---

async def schedule_user_jobs(chat_id: int, context: ContextTypes.DEFAULT_TYPE | Application):
    job_queue = context.job_queue
    for job in job_queue.get_jobs_by_name(f"user_{chat_id}"):
        job.schedule_removal()

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT morning, day, evening FROM schedule WHERE chat_id=?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                times = {"morning": row[0], "day": row[1], "evening": row[2]}
                for period, t_str in times.items():
                    if t_str != "OFF":
                        t_obj = datetime.strptime(t_str, "%H:%M").time().replace(tzinfo=MSK_TZ)
                        job_queue.run_daily(send_reminder, t_obj, chat_id=chat_id, 
                                          name=f"user_{chat_id}", data={"type": "bp", "period": period})

        async with db.execute("SELECT id, name, dosage, reminder_time FROM medications WHERE chat_id=?", (chat_id,)) as cursor:
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
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    period = context.job.data.get("period", "")
    async with aiosqlite.connect(DB_NAME) as db:
        one_hour_ago = (datetime.now(MSK_TZ) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        async with db.execute("SELECT timestamp FROM records WHERE chat_id=? AND timestamp > ? LIMIT 1", (chat_id, one_hour_ago)) as cursor:
            if await cursor.fetchone(): return

    period_names = {"morning": "утреннего", "day": "дневного", "evening": "вечернего"}
    await context.bot.send_message(chat_id=chat_id, text=f"⏰ Время для {period_names.get(period, '')} замера давления!")

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO schedule (chat_id, morning, day, evening) VALUES (?, ?, ?, ?)",
                        (chat_id, DEFAULT_MORNING, DEFAULT_DAY, DEFAULT_EVENING))
        await db.commit()
    await schedule_user_jobs(chat_id, context)
    await update.message.reply_text("Привет! Я бот для отслеживания давления. Отправьте замер (например, 120/80), /settings для настроек или /med_add для лекарств.", parse_mode="HTML")

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🌅 Утро", callback_data="set_morning"), InlineKeyboardButton("☀️ День", callback_data="set_day"), InlineKeyboardButton("🌙 Вечер", callback_data="set_evening")],
        [InlineKeyboardButton("❌ Отключить утро", callback_data="off_morning")],
        [InlineKeyboardButton("❌ Отключить день", callback_data="off_day")],
        [InlineKeyboardButton("❌ Отключить вечер", callback_data="off_evening")]
    ]
    await update.message.reply_text("Настройки напоминаний (МСК):", reply_markup=InlineKeyboardMarkup(keyboard))

async def med_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_med_name"] = True
    await update.message.reply_text("Введите название лекарства:")

async def med_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name, dosage, reminder_time FROM medications WHERE chat_id=?", (chat_id,)) as cursor:
            meds = await cursor.fetchall()
    if not meds:
        await update.message.reply_text("Список пуст. Используйте /med_add")
        return
    text = "💊 <b>Ваши лекарства:</b>\n\n"
    keyboard = []
    for m_id, name, dose, r_time in meds:
        text += f"• {name} ({dose}) — {r_time}\n"
        keyboard.append([InlineKeyboardButton(f"❌ Удалить {name}", callback_data=f"del_med_{m_id}")])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def universal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data, chat_id = query.data, query.message.chat_id
    if data.startswith("set_"):
        context.user_data["waiting_for_time"] = data.split("_")[1]
        await query.edit_message_text(f"Введите время (ЧЧ:ММ):")
    elif data.startswith("off_"):
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(f"UPDATE schedule SET {data.split('_')[1]}=? WHERE chat_id=?", ("OFF", chat_id))
            await db.commit()
        await schedule_user_jobs(chat_id, context)
        await query.edit_message_text("✅ Отключено.")
    elif data.startswith("take_"):
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO med_intake VALUES (?, ?, ?)", (chat_id, data.split("_")[1], datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M")))
            await db.commit()
        await query.edit_message_text("✅ Принято.")
    elif data.startswith("del_med_"):
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM medications WHERE id=?", (data.split("_")[2],))
            await db.commit()
        await schedule_user_jobs(chat_id, context)
        await query.edit_message_text("✅ Удалено.")

async def log_measurement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, text = update.message.chat_id, update.message.text.strip()
    if "waiting_for_time" in context.user_data:
        period = context.user_data.pop("waiting_for_time")
        try:
            datetime.strptime(text, "%H:%M")
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(f"UPDATE schedule SET {period}=? WHERE chat_id=?", (text, chat_id))
                await db.commit()
            await schedule_user_jobs(chat_id, context)
            await update.message.reply_text(f"✅ Установлено на {text}")
        except: await update.message.reply_text("❌ Ошибка формата.")
        return
    if "waiting_for_med_name" in context.user_data:
        context.user_data["med_name"] = text
        context.user_data.pop("waiting_for_med_name")
        context.user_data["waiting_for_med_dose"] = True
        await update.message.reply_text("Введите дозировку:")
        return
    if "waiting_for_med_dose" in context.user_data:
        context.user_data["med_dose"] = text
        context.user_data.pop("waiting_for_med_dose")
        context.user_data["waiting_for_med_time"] = True
        await update.message.reply_text("Введите время (ЧЧ:ММ):")
        return
    if "waiting_for_med_time" in context.user_data:
        try:
            datetime.strptime(text, "%H:%M")
            name, dose = context.user_data.pop("med_name"), context.user_data.pop("med_dose")
            context.user_data.pop("waiting_for_med_time")
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("INSERT INTO medications (chat_id, name, dosage, reminder_time) VALUES (?, ?, ?, ?)", (chat_id, name, dose, text))
                await db.commit()
            await schedule_user_jobs(chat_id, context)
            await update.message.reply_text("✅ Добавлено.")
        except: await update.message.reply_text("❌ Ошибка формата.")
        return

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
        async with db.execute("SELECT COUNT(*) FROM records WHERE chat_id=?", (chat_id,)) as c:
            if (await c.fetchone())[0] % 15 == 0:
                new_base = await calculate_median_baseline(chat_id)
                if new_base:
                    await db.execute("INSERT OR REPLACE INTO users_profile (chat_id, working_sys, working_dia, baseline_updated_at) VALUES (?, ?, ?, ?)", (chat_id, new_base[0], new_base[1], timestamp))
                    status += f"\n\n🤖 Норма обновлена: {new_base[0]}/{new_base[1]}"
        await db.execute("DELETE FROM records WHERE chat_id=? AND timestamp < ?", (chat_id, (now_msk - timedelta(days=14)).strftime("%Y-%m-%d %H:%M")))
        await db.commit()

    keyboard = []
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM medications WHERE chat_id=?", (chat_id,)) as cursor:
            async for m_id, m_name in cursor:
                keyboard.append([InlineKeyboardButton(f"💊 Принял {m_name}", callback_data=f"take_{m_id}")])

    await update.message.reply_text(f"✅ <b>Записано:</b> {sys}/{dia}\n📊 <b>Статус:</b> {status}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int):
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT timestamp FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 1", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await update.message.reply_text("Нет записей.")
                return
            start_date = (datetime.strptime(row[0], "%Y-%m-%d %H:%M") - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00")
            async with db.execute("SELECT timestamp, measurement FROM records WHERE chat_id=? AND timestamp >= ? ORDER BY timestamp ASC", (chat_id, start_date)) as cursor:
                records = await cursor.fetchall()
    
    if not records:
        await update.message.reply_text("Нет записей за период.")
        return
    res = f"📊 <b>Статистика ({days} дн.):</b>\n\n"
    for ts, m in records:
        res += f"🔹 {ts[5:]} — {m}\n"
    await update.message.reply_text(res, parse_mode="HTML")

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT timestamp, measurement FROM records WHERE chat_id=? ORDER BY timestamp ASC", (chat_id,)) as c1:
            r1 = await c1.fetchall()
        async with db.execute("SELECT i.timestamp, m.name, m.dosage FROM med_intake i JOIN medications m ON i.med_id = m.id WHERE i.chat_id=? ORDER BY i.timestamp ASC", (chat_id,)) as c2:
            r2 = await c2.fetchall()
    if not r1 and not r2:
        await update.message.reply_text("Нет данных.")
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["--- ЗАМЕРЫ ---"])
    writer.writerows(r1)
    writer.writerow(["--- ЛЕКАРСТВА ---"])
    writer.writerows(r2)
    output.seek(0)
    await context.bot.send_document(chat_id=chat_id, document=io.BytesIO(output.getvalue().encode('utf-8')), filename="history.csv")

async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT rowid, measurement FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 1", (chat_id,)) as c:
            row = await c.fetchone()
            if row:
                await db.execute("DELETE FROM records WHERE rowid=?", (row[0],))
                await db.commit()
                await update.message.reply_text(f"🗑 Удалено: {row[1]}")
            else: await update.message.reply_text("Нечего удалять.")

async def post_init(application: Application):
    await init_db()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id FROM schedule") as cursor:
            async for (chat_id,) in cursor:
                await schedule_user_jobs(chat_id, application)

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
