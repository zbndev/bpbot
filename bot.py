import os
import aiosqlite
import re
import logging
import csv
import io
import statistics
import shutil
from datetime import datetime, timedelta
from functools import partial
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
DB_NAME = "./db/bp_tracker.db"

# Whitelist для SQL-полей (защита от инъекций)
VALID_SCHEDULE_FIELDS = {"morning", "day", "evening"}


# --- БД (aiosqlite) ---
async def init_db():
    """Инициализация таблиц базы данных."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Создаём таблицу records с колонкой wellbeing
        await db.execute(
            "CREATE TABLE IF NOT EXISTS records (chat_id INTEGER, timestamp DATETIME, measurement TEXT, wellbeing TEXT)"
        )
        # Добавляем колонку wellbeing, если её нет (для существующих БД)
        try:
            await db.execute("ALTER TABLE records ADD COLUMN wellbeing TEXT")
        except aiosqlite.OperationalError:
            pass  # Колонка уже существует
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schedule (chat_id INTEGER PRIMARY KEY, morning TEXT, day TEXT, evening TEXT)"
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users_profile 
            (chat_id INTEGER PRIMARY KEY, working_sys INTEGER, working_dia INTEGER, 
            is_auto_baseline BOOLEAN DEFAULT 1, baseline_updated_at DATETIME)"""
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS medications (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, name TEXT, dosage TEXT, reminder_time TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS med_intake (chat_id INTEGER, med_id INTEGER, timestamp DATETIME)"
        )
        await db.commit()


# --- ЛОГИКА НОРМЫ ---
async def get_user_baseline_info(chat_id: int):
    """Получить рабочую норму давления пользователя."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT working_sys, working_dia, is_auto_baseline FROM users_profile WHERE chat_id=?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0], row[1], row[2]
    return 120, 80, 1


async def calculate_median_baseline(chat_id: int):
    """Вычислить медианную норму давления на основе последних 15 замеров."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT measurement FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 15",
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    if len(rows) < 10:
        return None

    sys_vals = []
    dia_vals = []

    for (measurement,) in rows:
        match = re.match(r"(\d{2,3})/(\d{2,3})", measurement)
        if match:
            sys_vals.append(int(match.group(1)))
            dia_vals.append(int(match.group(2)))

    if not sys_vals or not dia_vals:
        return None

    return int(statistics.median(sys_vals)), int(statistics.median(dia_vals))


def classify_bp(sys_val: int, dia_val: int, base_sys: int, base_dia: int) -> str:
    """Классифицировать уровень давления относительно нормы."""
    sys_diff = (sys_val - base_sys) / base_sys

    if sys_val >= 160 or dia_val >= 100:
        return "🔴 Крит. высокая"
    if sys_val >= 140 or dia_val >= 90:
        return "🟠 Высокое"
    if sys_diff > 0.15:
        return "🟡 Повышенное"
    if sys_diff < -0.15:
        return "🔵 Пониженное"
    return "🟢 В норме"


# --- ПЛАНИРОВЩИК ---
async def schedule_user_jobs(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE | Application
):
    """Запланировать напоминания для пользователя."""
    job_queue = context.job_queue

    # Удаляем старые задания пользователя
    for job in job_queue.get_jobs_by_name(f"user_{chat_id}"):
        job.schedule_removal()

    async with aiosqlite.connect(DB_NAME) as db:
        # Загружаем расписание замеров
        async with db.execute(
            "SELECT morning, day, evening FROM schedule WHERE chat_id=?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                for period_idx, period_name in enumerate(["morning", "day", "evening"]):
                    if row[period_idx] != "OFF":
                        time_obj = (
                            datetime.strptime(row[period_idx], "%H:%M")
                            .time()
                            .replace(tzinfo=MSK_TZ)
                        )
                        job_queue.run_daily(
                            send_reminder,
                            time_obj,
                            chat_id=chat_id,
                            name=f"user_{chat_id}",
                            data={"type": "bp", "period": period_name},
                        )

        # Загружаем напоминания о лекарствах
        async with db.execute(
            "SELECT id, name, dosage, reminder_time FROM medications WHERE chat_id=?",
            (chat_id,),
        ) as cursor:
            async for med_id, med_name, med_dosage, reminder_time in cursor:
                time_obj = (
                    datetime.strptime(reminder_time, "%H:%M")
                    .time()
                    .replace(tzinfo=MSK_TZ)
                )
                job_queue.run_daily(
                    send_med_reminder,
                    time_obj,
                    chat_id=chat_id,
                    name=f"user_{chat_id}",
                    data={"id": med_id, "name": med_name, "dose": med_dosage},
                )

    # Еженедельный отчёт каждое воскресенье в 20:00 МСК
    sunday_time = datetime.strptime("20:00", "%H:%M").time().replace(tzinfo=MSK_TZ)
    job_queue.run_daily(
        send_weekly_report,
        sunday_time,
        days=(6,),  # 6 = воскресенье (0=понедельник)
        chat_id=chat_id,
        name=f"weekly_{chat_id}",
        data={"type": "weekly_report"},
    )


async def send_med_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Отправить напоминание о приёме лекарства."""
    job_data = context.job.data
    chat_id = context.job.chat_id

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"💊 Пора принять: <b>{job_data['name']}</b> ({job_data['dose']})",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Принял", callback_data=f"take_{job_data['id']}"
                        )
                    ]
                ]
            ),
        )
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания о лекарстве: {e}")


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Отправить напоминание о замере давления."""
    chat_id = context.job.chat_id
    period = context.job.data.get("period", "")

    # Проверяем, не было ли замера за последний час
    async with aiosqlite.connect(DB_NAME) as db:
        one_hour_ago = (datetime.now(MSK_TZ) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M"
        )
        async with db.execute(
            "SELECT timestamp FROM records WHERE chat_id=? AND timestamp > ? LIMIT 1",
            (chat_id, one_hour_ago),
        ) as cursor:
            if await cursor.fetchone():
                return  # Недавний замер уже есть

    period_names = {"morning": "утреннего", "day": "дневного", "evening": "вечернего"}

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Время для {period_names.get(period, '')} замера давления!",
        )
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания о замере: {e}")


async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Отправить еженедельный отчёт пользователю."""
    chat_id = context.job.chat_id
    now_msk = datetime.now(MSK_TZ)
    week_ago = (now_msk - timedelta(days=7)).strftime("%Y-%m-%d 00:00")
    cutoff_14_days = (now_msk - timedelta(days=14)).strftime("%Y-%m-%d %H:%M")

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            # Подсчёт замеров за неделю
            async with db.execute(
                "SELECT COUNT(*) FROM records WHERE chat_id=? AND timestamp >= ?",
                (chat_id, week_ago),
            ) as cursor:
                bp_count_row = await cursor.fetchone()
                bp_count = bp_count_row[0] if bp_count_row else 0

            # Подсчёт приёмов лекарств за неделю
            async with db.execute(
                "SELECT COUNT(*) FROM med_intake WHERE chat_id=? AND timestamp >= ?",
                (chat_id, week_ago),
            ) as cursor:
                med_count_row = await cursor.fetchone()
                med_count = med_count_row[0] if med_count_row else 0

            # Подсчёт записей старше 14 дней
            async with db.execute(
                "SELECT COUNT(*) FROM records WHERE chat_id=? AND timestamp < ?",
                (chat_id, cutoff_14_days),
            ) as cursor:
                old_count_row = await cursor.fetchone()
                old_count = old_count_row[0] if old_count_row else 0

            # Подсчёт самочувствия за неделю
            async with db.execute(
                "SELECT wellbeing, COUNT(*) FROM records WHERE chat_id=? AND timestamp >= ? AND wellbeing IS NOT NULL GROUP BY wellbeing",
                (chat_id, week_ago),
            ) as cursor:
                wellbeing_rows = await cursor.fetchall()

            # Анализ: связь высокого давления с плохим самочувствием
            async with db.execute(
                """SELECT COUNT(*) FROM records r1 
                WHERE chat_id=? AND timestamp >= ? AND wellbeing = 'bad'
                AND EXISTS (
                    SELECT 1 FROM records r2 
                    WHERE r2.chat_id = r1.chat_id 
                    AND r2.measurement >= '140' 
                    AND r2.timestamp >= ?
                )""",
                (chat_id, week_ago, week_ago),
            ) as cursor:
                high_bp_bad_feel_row = await cursor.fetchone()
                high_bp_bad_feel = (
                    high_bp_bad_feel_row[0] if high_bp_bad_feel_row else 0
                )

        # Формируем текст отчёта
        report_lines = [
            "📊 <b>Еженедельный отчёт</b>\n",
            f"• Замеров за неделю: {bp_count}",
            f"• Принято лекарств: {med_count}",
        ]

        # Добавляем статистику самочувствия
        if wellbeing_rows:
            feeling_map = {"good": "😊", "ok": "😐", "bad": "☹️"}
            feeling_names = {"good": "Хорошо", "ok": "Нормально", "bad": "Плохо"}
            total_feelings = sum(count for _, count in wellbeing_rows)

            report_lines.append("\n📈 <b>Самочувствие за неделю:</b>")
            for feeling, count in sorted(
                wellbeing_rows, key=lambda x: x[1], reverse=True
            ):
                emoji = feeling_map.get(feeling, "")
                name = feeling_names.get(feeling, feeling)
                percent = (
                    round(count / total_feelings * 100) if total_feelings > 0 else 0
                )
                report_lines.append(f"  {emoji} {name}: {count} раз ({percent}%)")

        if old_count > 0:
            report_lines.append(f"\n• Записей старше 14 дней: {old_count} ⚠️")
            report_lines.append(
                "\n⚠️ <b>Внимание:</b> записи старше 14 дней автоматически удаляются.\n"
                "Сохраните их, если они вам нужны."
            )

        keyboard = [
            [
                InlineKeyboardButton(
                    "📥 Скачать все данные (CSV)", callback_data="export_csv"
                )
            ]
        ]

        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(report_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logger.error(f"Ошибка отправки еженедельного отчёта: {e}")


# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO schedule VALUES (?, ?, ?, ?)",
            (chat_id, DEFAULT_MORNING, DEFAULT_DAY, DEFAULT_EVENING),
        )
        await db.commit()

    await schedule_user_jobs(chat_id, context)

    welcome_text = (
        "👋 <b>Привет!</b> Я бот для отслеживания артериального давления и приёма лекарств.\n\n"
        "📝 <b>КАК ЗАПИСАТЬ ДАВЛЕНИЕ:</b>\n"
        "Просто отправьте сообщение в любом формате:\n"
        "• <code>120/80</code>\n"
        "• <code>120 80</code>\n"
        "• <code>120/80 65</code> (с пульсом)\n\n"
        "🎯 <b>АВТОМАТИЧЕСКАЯ РАБОЧАЯ НОРМА ДАВЛЕНИЯ:</b>\n"
        "Каждые 15 замеров я предлагаю обновить вашу «рабочую норму давления» на основе медианы.\n"
        "Также можно установить вручную в /settings.\n\n"
        "📊 <b>КЛАССИФИКАЦИЯ ДАВЛЕНИЯ:</b>\n"
        "Я автоматически определяю статус относительно вашей нормы:\n"
        "• 🟢 В норме\n"
        "• 🟡 Повышенное\n"
        "• 🟠 Высокое\n"
        "• 🔴 Крит. высокая\n"
        "• 🔵 Пониженное\n\n"
        "😊 <b>САМОЧУВСТВИЕ:</b>\n"
        "После каждого замера вы можете отметить как себя чувствуете:\n"
        "• 😊 Хорошо\n"
        "• 😐 Нормально\n"
        "• ☹️ Плохо\n"
        "Статистика самочувствия включена в еженедельный отчёт.\n\n"
        "💊 <b>ЛЕКАРСТВА:</b>\n"
        "• /med_add — добавить лекарство с напоминанием\n"
        "• /med_list — список ваших лекарств\n"
        "После записи давления появляются кнопки для отметки приёма.\n\n"
        "⏰ <b>НАПОМИНАНИЯ:</b>\n"
        "• /settings — настроить время напоминаний (утро/день/вечер)\n"
        "• По умолчанию: 08:00, 14:00, 20:00 (МСК)\n\n"
        "📈 <b>СТАТИСТИКА:</b>\n"
        "• /stats_3 — за последние 3 дня\n"
        "• /stats_7 — за последние 7 дней\n"
        "• /export — скачать всю историю (CSV)\n\n"
        "🗑 <b>УПРАВЛЕНИЕ ЗАПИСЯМИ:</b>\n"
        "• /delete_last — удалить последнюю запись\n\n"
        "⚠️ <i>Записи старше 14 дней автоматически удаляются. Каждое воскресенье в 20:00 приходит еженедельный отчёт с возможностью сохранить данные.</i>"
    )
    await update.effective_message.reply_text(welcome_text, parse_mode="HTML")


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню настроек."""
    keyboard = [
        [
            InlineKeyboardButton("🌅 Утро", callback_data="set_morning"),
            InlineKeyboardButton("☀️ День", callback_data="set_day"),
            InlineKeyboardButton("🌙 Вечер", callback_data="set_evening"),
        ],
        [
            InlineKeyboardButton("❌ Откл. Утро", callback_data="off_morning"),
            InlineKeyboardButton("❌ Откл. День", callback_data="off_day"),
            InlineKeyboardButton("❌ Откл. Вечер", callback_data="off_evening"),
        ],
        [
            InlineKeyboardButton(
                "🎯 Установить норму давления", callback_data="set_baseline"
            )
        ],
    ]
    await update.effective_message.reply_text(
        "Настройки напоминаний (МСК) и нормы давления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def med_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс добавления лекарства."""
    context.user_data["waiting_for"] = "med_name"
    await update.effective_message.reply_text("Введите название лекарства:")


async def db_backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс резервного копирования БД."""
    db_password = os.getenv("DB_PASSWORD")
    if not db_password:
        await update.effective_message.reply_text("❌ Функция резервного копирования не настроена.")
        return
    
    context.user_data["waiting_for"] = "db_password"
    await update.effective_message.reply_text("Введите пароль:")


async def handle_db_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка пароля для доступа к БД."""
    password = update.message.text
    db_password = os.getenv("DB_PASSWORD")
    
    if password == db_password:
        context.user_data.pop("waiting_for", None)
        context.user_data["db_authenticated"] = True
        keyboard = [
            [InlineKeyboardButton("📥 Скачать", callback_data="db_download")],
            [InlineKeyboardButton("📤 Восстановить", callback_data="db_upload")],
        ]
        await update.effective_message.reply_text(
            "✅ Доступ разрешён. Выберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        context.user_data.pop("waiting_for", None)
        await update.effective_message.reply_text("❌ Неверный пароль.")


async def handle_db_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправить файл БД пользователю."""
    query = update.callback_query
    await query.answer()
    
    if not context.user_data.get("db_authenticated"):
        await query.edit_message_text("❌ Сессия истекла. Используйте /db для повторной авторизации.")
        return
    
    if not os.path.exists(DB_NAME):
        await query.edit_message_text("❌ Файл базы данных не найден.")
        return
    
    try:
        with open(DB_NAME, "rb") as db_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=db_file,
                filename="bp_tracker.db",
                caption="📦 Файл базы данных"
            )
    except Exception as e:
        logger.error(f"Error sending database file: {e}")
        await query.edit_message_text("❌ Ошибка при отправке файла.")


async def handle_db_upload_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запросить файл БД от пользователя."""
    query = update.callback_query
    await query.answer()
    
    if not context.user_data.get("db_authenticated"):
        await query.edit_message_text("❌ Сессия истекла. Используйте /db для повторной авторизации.")
        return
    
    context.user_data["waiting_for_db_file"] = True
    await query.edit_message_text("Отправьте файл БД:")


async def handle_db_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработать загруженный файл БД."""
    if not context.user_data.get("waiting_for_db_file"):
        return
    
    context.user_data.pop("waiting_for_db_file", None)
    context.user_data.pop("db_authenticated", None)
    
    document = update.message.document
    if not document:
        await update.effective_message.reply_text("❌ Файл не найден.")
        return
    
    if document.file_name != "bp_tracker.db":
        await update.effective_message.reply_text("❌ Неверное имя файла. Ожидается: bp_tracker.db")
        return
    
    try:
        # Скачиваем файл
        new_file = await context.bot.get_file(document.file_id)
        
        # Создаём резервную копию текущей БД
        backup_path = f"{DB_NAME}.backup"
        if os.path.exists(DB_NAME):
            shutil.copy2(DB_NAME, backup_path)
        
        # Скачиваем и сохраняем новый файл
        await new_file.download_to_drive(DB_NAME)
        
        # Проверяем, что файл валидный SQLite
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        
        # Удаляем резервную копию при успехе
        if os.path.exists(backup_path):
            os.remove(backup_path)
        
        await update.effective_message.reply_text("✅ База данных успешно восстановлена.")
        
        # Перепланируем задания для всех пользователей
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT chat_id FROM schedule") as cursor:
                async for (chat_id,) in cursor:
                    await schedule_user_jobs(chat_id, context)
        
    except aiosqlite.Error:
        # Восстанавливаем резервную копию при ошибке
        if os.path.exists(f"{DB_NAME}.backup"):
            shutil.copy2(f"{DB_NAME}.backup", DB_NAME)
            os.remove(f"{DB_NAME}.backup")
        await update.effective_message.reply_text("❌ Загруженный файл не является валидной базой данных SQLite.")
    except Exception as e:
        logger.error(f"Error uploading database file: {e}")
        # Восстанавливаем резервную копию при ошибке
        if os.path.exists(f"{DB_NAME}.backup"):
            shutil.copy2(f"{DB_NAME}.backup", DB_NAME)
            os.remove(f"{DB_NAME}.backup")
        await update.effective_message.reply_text(f"❌ Ошибка при восстановлении: {e}")


async def med_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список лекарств пользователя."""
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, name, dosage, reminder_time FROM medications WHERE chat_id=?",
            (chat_id,),
        ) as cursor:
            meds = await cursor.fetchall()

    if not meds:
        await update.effective_message.reply_text("Список лекарств пуст.")
        return

    text = "💊 <b>Ваши лекарства:</b>\n\n"
    keyboard = []

    for med_id, med_name, med_dosage, reminder_time in meds:
        text += f"• {med_name} ({med_dosage}) — {reminder_time} МСК\n"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"❌ Удалить {med_name}", callback_data=f"del_med_{med_id}"
                )
            ]
        )

    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def universal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальный обработчик callback-кнопок."""
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = update.effective_chat.id

    try:
        if data.startswith("set_"):
            mode = data.split("_")[1]
            context.user_data["waiting_for"] = mode
            if mode == "baseline":
                await query.edit_message_text("Введите норму (120/80):")
            else:
                await query.edit_message_text("Введите время (ЧЧ:ММ):")

        elif data.startswith("off_"):
            field = data.split("_")[1]
            if field not in VALID_SCHEDULE_FIELDS:
                await query.edit_message_text("❌ Ошибка: неверное поле.")
                return

            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    f"UPDATE schedule SET {field}=? WHERE chat_id=?", ("OFF", chat_id)
                )
                await db.commit()

            await schedule_user_jobs(chat_id, context)
            await query.edit_message_text("✅ Отключено.")

        elif data.startswith("take_"):
            med_id = data.split("_")[1]
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    "INSERT INTO med_intake VALUES (?, ?, ?)",
                    (chat_id, med_id, datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M")),
                )
                await db.commit()
            await query.edit_message_text("✅ Отметка о приеме сохранена.")

        elif data.startswith("del_med_"):
            med_id = data.split("_")[2]
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("DELETE FROM medications WHERE id=?", (med_id,))
                await db.commit()
            await schedule_user_jobs(chat_id, context)
            await query.edit_message_text("✅ Удалено.")

        elif data.startswith("apply_base_"):
            parts = data.split("_")
            new_sys = parts[2]
            new_dia = parts[3]
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO users_profile 
                    (chat_id, working_sys, working_dia, is_auto_baseline, baseline_updated_at) 
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        chat_id,
                        new_sys,
                        new_dia,
                        1,
                        datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M"),
                    ),
                )
                await db.commit()
            await query.edit_message_text(f"✅ Норма обновлена до {new_sys}/{new_dia}.")

        # --- Обработка самочувствия ---
        elif data.startswith("feel_"):
            parts = data.split("_")
            feeling = parts[1]  # good, ok, bad
            rowid = parts[2] if len(parts) > 2 else None

            feeling_map = {"good": "😊", "ok": "😐", "bad": "☹️"}
            feeling_emoji = feeling_map.get(feeling, "")

            if rowid:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "UPDATE records SET wellbeing=? WHERE rowid=?", (feeling, rowid)
                    )
                    await db.commit()

            # Обновляем сообщение, убираем кнопки самочувствия
            original_text = query.message.text or ""
            if "💬 Как вы себя чувствуете?" in original_text:
                new_text = original_text.replace(
                    "\n\n💬 Как вы себя чувствуете?", f" {feeling_emoji}"
                )
            else:
                new_text = original_text + f" {feeling_emoji}"

            # Оставляем только кнопки лекарств и обновления нормы (если есть)
            try:
                await query.edit_message_text(new_text, parse_mode="HTML")
            except:
                pass  # Текст не изменился

            await query.answer(f"Записано: {feeling_emoji}")

        # --- Обработка удаления записей ---
        elif data == "del_bp":
            rowid = context.user_data.pop("delete_bp_rowid", None)
            value = context.user_data.pop("delete_bp_value", "")
            # Очищаем остальные данные
            context.user_data.pop("delete_med_rowid", None)
            context.user_data.pop("delete_med_name", None)

            if rowid:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("DELETE FROM records WHERE rowid=?", (rowid,))
                    await db.commit()
                await query.edit_message_text(f"🗑 Удалено: {value}")
            else:
                await query.edit_message_text("❌ Запись не найдена.")

        elif data == "del_med":
            rowid = context.user_data.pop("delete_med_rowid", None)
            med_name = context.user_data.pop("delete_med_name", "")
            # Очищаем остальные данные
            context.user_data.pop("delete_bp_rowid", None)
            context.user_data.pop("delete_bp_value", None)

            if rowid:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("DELETE FROM med_intake WHERE rowid=?", (rowid,))
                    await db.commit()
                await query.edit_message_text(f"🗑 Удалён приём: {med_name}")
            else:
                await query.edit_message_text("❌ Запись не найдена.")

        elif data == "del_cancel":
            # Очищаем все данные удаления
            context.user_data.pop("delete_bp_rowid", None)
            context.user_data.pop("delete_bp_value", None)
            context.user_data.pop("delete_med_rowid", None)
            context.user_data.pop("delete_med_name", None)
            await query.edit_message_text("❌ Отменено.")

        # --- Экспорт из еженедельного отчёта ---
        elif data == "export_csv":
            await query.edit_message_text("📥 Подготовка файла...")

            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
                    "SELECT timestamp, measurement FROM records WHERE chat_id=? ORDER BY timestamp ASC",
                    (chat_id,),
                ) as cursor:
                    bp_records = await cursor.fetchall()

                async with db.execute(
                    """SELECT i.timestamp, m.name, m.dosage 
                    FROM med_intake i 
                    JOIN medications m ON i.med_id = m.id 
                    WHERE i.chat_id=? 
                    ORDER BY i.timestamp ASC""",
                    (chat_id,),
                ) as cursor:
                    med_records = await cursor.fetchall()

            output = io.StringIO()
            writer = csv.writer(output)

            writer.writerow(["--- ЗАМЕРЫ ---"])
            writer.writerow(["Дата и время", "Показания"])
            writer.writerows(bp_records)
            writer.writerow([])
            writer.writerow(["--- ЛЕКАРСТВА ---"])
            writer.writerow(["Дата и время", "Название", "Доза"])
            writer.writerows(med_records)

            output.seek(0)
            await context.bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(output.getvalue().encode("utf-8")),
                filename="history.csv",
            )
            await query.edit_message_text("✅ Файл отправлен.")

        # --- Резервное копирование БД ---
        elif data == "db_download":
            await handle_db_download(update, context)

        elif data == "db_upload":
            await handle_db_upload_request(update, context)

    except Exception as e:
        logger.error(f"Ошибка в callback обработчике: {e}")
        await query.edit_message_text("❌ Произошла ошибка.")


async def log_measurement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений — запись измерений и многоступенчатые диалоги."""
    chat_id = update.effective_chat.id

    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    wait_mode = context.user_data.get("waiting_for")

    # --- Обработка ввода пароля для БД ---
    if wait_mode == "db_password":
        await handle_db_password(update, context)
        return

    # --- Обработка ввода нормы давления ---
    if wait_mode == "baseline":
        match = re.match(r"^(\d{2,3})[^\d]+(\d{2,3})$", text)
        if match:
            sys_val, dia_val = map(int, match.groups())
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO users_profile 
                    (chat_id, working_sys, working_dia, is_auto_baseline, baseline_updated_at) 
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        chat_id,
                        sys_val,
                        dia_val,
                        0,
                        datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M"),
                    ),
                )
                await db.commit()
            context.user_data.pop("waiting_for")
            await update.effective_message.reply_text(
                f"✅ Ваша норма {sys_val}/{dia_val} сохранена (вручную)."
            )
        else:
            await update.effective_message.reply_text("❌ Формат: 120/80.")
        return

    # --- Обработка ввода времени напоминания ---
    elif wait_mode in ["morning", "day", "evening"]:
        if wait_mode not in VALID_SCHEDULE_FIELDS:
            context.user_data.pop("waiting_for", None)
            return

        try:
            datetime.strptime(text, "%H:%M")
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    f"UPDATE schedule SET {wait_mode}=? WHERE chat_id=?",
                    (text, chat_id),
                )
                await db.commit()
            await schedule_user_jobs(chat_id, context)
            context.user_data.pop("waiting_for")
            await update.effective_message.reply_text(
                f"✅ Напоминание установлено на {text}."
            )
        except ValueError:
            await update.effective_message.reply_text("❌ Формат: ЧЧ:ММ.")
        return

    # --- Многоступенчатый диалог добавления лекарства ---
    elif wait_mode == "med_name":
        context.user_data["med_name"] = text
        context.user_data["waiting_for"] = "med_dose"
        await update.effective_message.reply_text("Введите дозировку:")
        return

    elif wait_mode == "med_dose":
        context.user_data["med_dose"] = text
        context.user_data["waiting_for"] = "med_time"
        await update.effective_message.reply_text("Введите время напоминания (ЧЧ:ММ):")
        return

    elif wait_mode == "med_time":
        try:
            datetime.strptime(text, "%H:%M")
            med_name = context.user_data.pop("med_name")
            med_dose = context.user_data.pop("med_dose")

            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    "INSERT INTO medications (chat_id, name, dosage, reminder_time) VALUES (?, ?, ?, ?)",
                    (chat_id, med_name, med_dose, text),
                )
                await db.commit()

            await schedule_user_jobs(chat_id, context)
            context.user_data.pop("waiting_for")
            await update.effective_message.reply_text("✅ Лекарство добавлено.")
        except ValueError:
            await update.effective_message.reply_text(
                "❌ Ошибка времени. Используйте формат ЧЧ:ММ."
            )
        return

    # --- Запись измерения давления ---
    match = re.match(r"^(\d{2,3})[\s/-]+(\d{2,3})(?:[\s/-]+(\d{2,3}))?$", text)
    if not match:
        return

    sys_val, dia_val, pulse = map(int, match.groups(default=0))

    # Проверка реалистичности значений
    if not (50 <= sys_val <= 250 and 30 <= dia_val <= 150):
        await update.effective_message.reply_text(
            "⚠️ Цифры кажутся нереалистичными. Проверьте ввод."
        )
        return

    now_msk = datetime.now(MSK_TZ)
    timestamp = now_msk.strftime("%Y-%m-%d %H:%M")

    base_sys, base_dia, is_auto = await get_user_baseline_info(chat_id)
    status = classify_bp(sys_val, dia_val, base_sys, base_dia)

    measurement_str = f"{sys_val}/{dia_val}"
    if pulse:
        measurement_str += f" {pulse}"

    keyboard = []

    async with aiosqlite.connect(DB_NAME) as db:
        # Сохраняем запись (wellbeing пока NULL)
        await db.execute(
            "INSERT INTO records VALUES (?, ?, ?, NULL)",
            (chat_id, timestamp, measurement_str),
        )

        # Получаем rowid последней записи для сохранения самочувствия
        async with db.execute("SELECT last_insert_rowid()") as cursor:
            row = await cursor.fetchone()
            last_rowid = row[0] if row else None

        # Проверяем, нужно ли предложить обновить норму (каждые 15 записей)
        async with db.execute(
            "SELECT COUNT(*) FROM records WHERE chat_id=?", (chat_id,)
        ) as count_cursor:
            count_row = await count_cursor.fetchone()
            if count_row[0] % 15 == 0:
                new_baseline = await calculate_median_baseline(chat_id)
                if new_baseline:
                    new_base_sys, new_base_dia = new_baseline
                    if abs(new_base_sys - base_sys) / base_sys > 0.05:
                        status += f"\n\n🤖 <b>Совет:</b> Среднее за 15 замеров: {new_base_sys}/{new_base_dia}. Обновим вашу рабочую норму давления?"
                        keyboard.append(
                            [
                                InlineKeyboardButton(
                                    f"🔄 Обновить до {new_base_sys}/{new_base_dia}",
                                    callback_data=f"apply_base_{new_base_sys}_{new_base_dia}",
                                )
                            ]
                        )

        # Удаляем записи старше 14 дней
        cutoff_date = (now_msk - timedelta(days=14)).strftime("%Y-%m-%d %H:%M")
        await db.execute(
            "DELETE FROM records WHERE chat_id=? AND timestamp < ?",
            (chat_id, cutoff_date),
        )
        await db.commit()

    # Кнопки самочувствия (всегда показываются первыми)
    wellbeing_keyboard = [
        [
            InlineKeyboardButton("😊 Хорошо", callback_data=f"feel_good_{last_rowid}"),
            InlineKeyboardButton("😐 Нормально", callback_data=f"feel_ok_{last_rowid}"),
            InlineKeyboardButton("☹️ Плохо", callback_data=f"feel_bad_{last_rowid}"),
        ]
    ]

    # Добавляем кнопки для отметки приёма лекарств
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, name FROM medications WHERE chat_id=?", (chat_id,)
        ) as med_cursor:
            async for med_id, med_name in med_cursor:
                wellbeing_keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"💊 Принял {med_name}", callback_data=f"take_{med_id}"
                        )
                    ]
                )

    # Добавляем остальные кнопки (обновление нормы)
    wellbeing_keyboard.extend(keyboard)

    await update.effective_message.reply_text(
        f"✅ <b>Записано:</b> {sys_val}/{dia_val}\n📊 <b>Статус:</b> {status}\n\n💬 Как вы себя чувствуете?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(wellbeing_keyboard),
    )


async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int):
    """Получить статистику за указанное количество дней."""
    chat_id = update.effective_chat.id
    start_dt = (datetime.now(MSK_TZ) - timedelta(days=days)).strftime("%Y-%m-%d 00:00")

    # Маппинг самочувствия на эмодзи
    feeling_map = {"good": "😊", "ok": "😐", "bad": "☹️"}

    async with aiosqlite.connect(DB_NAME) as db:
        # Получаем записи давления с самочувствием
        async with db.execute(
            "SELECT timestamp, measurement, wellbeing FROM records WHERE chat_id=? AND timestamp >= ? ORDER BY timestamp ASC",
            (chat_id, start_dt),
        ) as cursor:
            bp_records = await cursor.fetchall()

        # Получаем записи приёма лекарств
        async with db.execute(
            """SELECT i.timestamp, m.name || ' (' || m.dosage || ')' 
            FROM med_intake i 
            JOIN medications m ON i.med_id = m.id 
            WHERE i.chat_id=? AND i.timestamp >= ? 
            ORDER BY i.timestamp ASC""",
            (chat_id, start_dt),
        ) as cursor:
            med_records = await cursor.fetchall()

        base_sys, base_dia, is_auto = await get_user_baseline_info(chat_id)

    if not bp_records and not med_records:
        await update.effective_message.reply_text("Нет данных.")
        return

    # Объединяем и сортируем события
    events = []
    for timestamp, value, wellbeing in bp_records:
        feeling_emoji = feeling_map.get(wellbeing, "") if wellbeing else ""
        events.append(
            (timestamp, f"🔹 {timestamp[5:16]} — <b>{value}</b> {feeling_emoji}")
        )
    for timestamp, value in med_records:
        events.append((timestamp, f"💊 {timestamp[5:16]} — {value}"))
    events.sort(key=lambda x: x[0])

    baseline_type = "авто" if is_auto else "ручная"
    result = (
        f"📊 <b>Статистика за {days} дн.</b>\n🎯 Норма: {base_sys}/{base_dia} ({baseline_type})\n"
        + "—" * 15
        + "\n"
    )
    result += "\n".join([event[1] for event in events])

    await update.effective_message.reply_text(result, parse_mode="HTML")


async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспортировать все данные пользователя в CSV."""
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT timestamp, measurement FROM records WHERE chat_id=? ORDER BY timestamp ASC",
            (chat_id,),
        ) as cursor:
            bp_records = await cursor.fetchall()

        async with db.execute(
            """SELECT i.timestamp, m.name, m.dosage 
            FROM med_intake i 
            JOIN medications m ON i.med_id = m.id 
            WHERE i.chat_id=? 
            ORDER BY i.timestamp ASC""",
            (chat_id,),
        ) as cursor:
            med_records = await cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    # Записываем замеры давления
    writer.writerow(["--- ЗАМЕРЫ ---"])
    writer.writerow(["Дата и время", "Показания"])
    writer.writerows(bp_records)

    writer.writerow([])

    # Записываем приём лекарств
    writer.writerow(["--- ЛЕКАРСТВА ---"])
    writer.writerow(["Дата и время", "Название", "Доза"])
    writer.writerows(med_records)

    output.seek(0)
    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(output.getvalue().encode("utf-8")),
        filename="history.csv",
    )


async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню удаления последних записей."""
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_NAME) as db:
        # Проверяем наличие записей давления
        async with db.execute(
            "SELECT rowid, measurement FROM records WHERE chat_id=? ORDER BY timestamp DESC LIMIT 1",
            (chat_id,),
        ) as cursor:
            bp_row = await cursor.fetchone()

        # Проверяем наличие записей о приёме лекарств
        async with db.execute(
            """SELECT mi.rowid, m.name, m.dosage 
            FROM med_intake mi 
            JOIN medications m ON mi.med_id = m.id 
            WHERE mi.chat_id=? 
            ORDER BY mi.timestamp DESC LIMIT 1""",
            (chat_id,),
        ) as cursor:
            med_row = await cursor.fetchone()

    has_bp = bp_row is not None
    has_med = med_row is not None

    # Если нет записей вообще
    if not has_bp and not has_med:
        await update.effective_message.reply_text("Нечего удалять.")
        return

    # Сохраняем данные для удаления в user_data
    keyboard = []

    if has_bp:
        context.user_data["delete_bp_rowid"] = bp_row[0]
        context.user_data["delete_bp_value"] = bp_row[1]

    if has_med:
        context.user_data["delete_med_rowid"] = med_row[0]
        context.user_data["delete_med_name"] = f"{med_row[1]} ({med_row[2]})"

    # Формируем клавиатуру в зависимости от наличия записей
    if has_bp and has_med:
        # Оба типа записей — показываем обе кнопки
        keyboard = [
            [
                InlineKeyboardButton(f"📊 {bp_row[1]}", callback_data="del_bp"),
                InlineKeyboardButton(f"💊 {med_row[1]}", callback_data="del_med"),
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")],
        ]
        message_text = "🗑 <b>Что удалить?</b>"
    elif has_bp:
        # Только запись давления
        keyboard = [
            [InlineKeyboardButton(f"🗑 Удалить {bp_row[1]}", callback_data="del_bp")],
            [InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")],
        ]
        message_text = f"🗑 <b>Удалить последнюю запись?</b>\n\n📊 {bp_row[1]}"
    else:
        # Только запись о лекарстве
        keyboard = [
            [InlineKeyboardButton(f"🗑 Удалить {med_row[1]}", callback_data="del_med")],
            [InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")],
        ]
        message_text = (
            f"🗑 <b>Удалить последнюю запись?</b>\n\n💊 {med_row[1]} ({med_row[2]})"
        )

    await update.effective_message.reply_text(
        message_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)


async def post_init(application: Application):
    """Инициализация после запуска приложения."""
    await init_db()

    # Восстанавливаем задания для всех пользователей
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id FROM schedule") as cursor:
            async for (chat_id,) in cursor:
                await schedule_user_jobs(chat_id, application)


if __name__ == "__main__":
    application = (
        Application.builder().token(os.getenv("TG_TOKEN")).post_init(post_init).build()
    )

    # Добавляем обработчик ошибок
    application.add_error_handler(error_handler)

    # Регистрируем команды
    commands = [
        ("start", start),
        ("settings", show_settings),
        ("med_add", med_add),
        ("med_list", med_list),
        ("delete_last", delete_last),
        ("export", export_data),
        ("db", db_backup_command),
    ]

    for cmd_name, cmd_handler in commands:
        application.add_handler(CommandHandler(cmd_name, cmd_handler))

    # Используем partial вместо lambda для async-функций
    application.add_handler(CommandHandler("stats_3", partial(get_stats, days=3)))
    application.add_handler(CommandHandler("stats_7", partial(get_stats, days=7)))

    # Обработчик callback-кнопок
    application.add_handler(CallbackQueryHandler(universal_callback))

    # Обработчик текстовых сообщений (кроме команд)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, log_measurement)
    )

    # Обработчик документов (для загрузки БД)
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_db_file_upload)
    )

    application.run_polling()

