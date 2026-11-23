import asyncio
import os
import socket
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape

import pytz
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from parsers import (
    BELL_SCHEDULE,
    SCHEDULE_URL,
    TTL_SCHEDULE,
    _group_lines,
    _parse_day,
    _soup,
    filter_by_week,
    get_bus_routes,
    get_bus_schedule,
    get_groups,
    get_substitutions,
    room_label,
    split_trailing_room,
    week_for_date,
)

# Токен береться зі змінної середовища BOT_TOKEN (не зберігай його в коді!)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задано змінну середовища BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# Підписи кнопок нижньої панелі
BTN_TODAY = "📅 Сьогодні"
BTN_TOMORROW = "📆 Завтра"
BTN_GROUP = "👥 Група"

DAYS_MAP = {0: "Понеділок", 1: "Вівторок", 2: "Середа", 3: "Четвер", 4: "П'ятниця"}


# ───────────────────────── База даних ─────────────────────────

def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        group_name TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        chat_id INTEGER PRIMARY KEY,
        message_ids TEXT
    )''')
    conn.commit()
    conn.close()


def set_user_group(user_id, group_name):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO users (user_id, group_name) VALUES (?, ?)', (user_id, group_name))
    conn.commit()
    conn.close()


def get_user_group(user_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT group_name FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def get_all_users():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, group_name FROM users')
    result = cursor.fetchall()
    conn.close()
    return result


# ───────────────────────── Чистий чат ─────────────────────────
# У чаті лишаються тільки актуальні повідомлення бота: команди користувача
# видаляються одразу, а попередні повідомлення бота — коли приходить нове.

_chat_locks = defaultdict(asyncio.Lock)


def get_tracked(chat_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT message_ids FROM chat_messages WHERE chat_id = ?', (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return [int(i) for i in row[0].split(',') if i] if row and row[0] else []


def set_tracked(chat_id, ids):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT OR REPLACE INTO chat_messages (chat_id, message_ids) VALUES (?, ?)',
        (chat_id, ','.join(str(i) for i in ids)),
    )
    conn.commit()
    conn.close()


async def safe_delete(chat_id, message_id):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass  # вже видалене або старше 48 годин


async def delete_user_message(message: Message):
    await safe_delete(message.chat.id, message.message_id)


async def show(chat_id, items):
    """
    Видаляє попередні актуальні повідомлення бота і надсилає нові.
    items — список (текст, параметри для send_message).
    """
    async with _chat_locks[chat_id]:
        for mid in get_tracked(chat_id):
            await safe_delete(chat_id, mid)
        set_tracked(chat_id, [])
        ids = []
        for i, (text, kwargs) in enumerate(items):
            if i == 0 and "reply_markup" not in kwargs:
                kwargs = {**kwargs, "reply_markup": get_main_menu()}
            msg = await bot.send_message(chat_id, text, **kwargs)
            ids.append(msg.message_id)
            set_tracked(chat_id, ids)


# ───────────────────────── Захист від дублів ─────────────────────────
# Якщо дві копії бота працюють на одному диску, кожне оновлення
# обробить тільки та копія, яка першою його "забрала".
# (Якщо копії на різних серверах, допоможе тільки відкликання токена.)

def _claim(table, key):
    """True, якщо ключ вперше; False, якщо його вже забрала інша копія."""
    conn = sqlite3.connect('dedupe.db', timeout=10)
    try:
        conn.execute(f'CREATE TABLE IF NOT EXISTS {table} (k TEXT PRIMARY KEY, ts REAL)')
        conn.execute(f'INSERT INTO {table} (k, ts) VALUES (?, ?)', (str(key), time.time()))
        conn.execute(f'DELETE FROM {table} WHERE ts < ?', (time.time() - 3 * 86400,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


class DedupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not _claim("updates", event.update_id):
            return None
        return await handler(event, data)


dp.update.outer_middleware(DedupMiddleware())


# ───────────────────────── Клавіатури ─────────────────────────

def get_main_menu():
    """Постійна панель знизу: сьогодні / завтра / вибір групи."""
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=BTN_TODAY),
            KeyboardButton(text=BTN_TOMORROW),
            KeyboardButton(text=BTN_GROUP),
        ]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Обери дію 👇",
    )


async def get_groups_keyboard():
    builder = InlineKeyboardBuilder()
    groups = await asyncio.to_thread(get_groups)
    for group in groups:
        builder.button(text=group, callback_data=f"group_{group}")
    builder.adjust(2)
    return builder.as_markup()


def get_bus_keyboard():
    builder = InlineKeyboardBuilder()
    for route in get_bus_routes():
        builder.button(text=f"№{route}", callback_data=f"bus_{route}")
    builder.adjust(4)
    return builder.as_markup()


def get_bus_back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🚌 Інші маршрути", callback_data="busmenu")
    return builder.as_markup()


# ───────────────────────── Команди ─────────────────────────

@dp.message(Command("start"))
@dp.message(F.text == BTN_GROUP)
async def cmd_start(message: Message):
    await delete_user_message(message)
    await show(message.chat.id, [
        ("Привіт! 👋 Команди завжди під рукою на панелі знизу.", {}),
        ("Обери свою групу, щоб я надсилав розклад:",
         {"reply_markup": await get_groups_keyboard()}),
    ])


@dp.callback_query(F.data.startswith('group_'))
async def process_group(callback: CallbackQuery):
    group_name = callback.data.split('_', 1)[1]
    set_user_group(callback.from_user.id, group_name)
    await callback.message.edit_text(
        f"✅ Ти обрав групу: {group_name}.\n\n"
        f"Тепер щодня о 18:00 я надсилатиму розклад на завтра.\n"
        f"Команди: /today — розклад на сьогодні, /tomorrow — на завтра."
    )


@dp.message(Command("today"))
@dp.message(F.text == BTN_TODAY)
async def cmd_today(message: Message):
    await send_schedule(message, offset=0)


@dp.message(Command("tomorrow"))
@dp.message(F.text == BTN_TOMORROW)
async def cmd_tomorrow(message: Message):
    await send_schedule(message, offset=1)


@dp.callback_query(F.data.startswith('bus_'))
async def process_bus(callback: CallbackQuery):
    route = callback.data.split('_', 1)[1]
    await callback.answer()
    # Редагуємо те саме повідомлення з кнопками: у чаті лишається лише розклад
    await callback.message.edit_text(f"Шукаю розклад для маршруту {route}...")
    schedule = await asyncio.to_thread(get_bus_schedule, route)
    await callback.message.edit_text(
        schedule,
        parse_mode="HTML",
        reply_markup=get_bus_back_keyboard(),
    )


@dp.callback_query(F.data == "busmenu")
async def process_bus_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "Переглянути розклад автобусів?",
        reply_markup=get_bus_keyboard(),
    )


@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    """Показує, що парсер реально бачить на сайті для твоєї групи."""
    await delete_user_message(message)
    group = get_user_group(message.from_user.id)
    if not group:
        await show(message.chat.id, [("Спочатку обери групу через /start", {})])
        return

    def work():
        return _group_lines(_soup(SCHEDULE_URL, TTL_SCHEDULE), group)

    try:
        lines = await asyncio.to_thread(work)
        subs = await asyncio.to_thread(get_substitutions, group)
        raw = "\n".join(" | ".join(r) for r in subs.get("raw", [])) or "—"
        text = (
            f"Група: {group}\n"
            f"Тиждень у замінах: {subs.get('week') or 'не знайдено'}\n"
            f"Рядки замін (сирі):\n{raw}\n\n"
            f"Рядків у блоці групи: {len(lines)}\n\n" + "\n".join(lines[:50])
        )
    except Exception as e:
        text = f"Помилка: {type(e).__name__}: {e}"
    await show(message.chat.id, [(text[:4000], {})])


@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await delete_user_message(message)
    await show(message.chat.id, [(f"pid={os.getpid()} host={socket.gethostname()}", {})])


# ───────────────────────── Збірка повідомлення ─────────────────────────

DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬"


def format_lesson(info):
    """
    «Предмет (70) | Викладач» ->
        Предмет
        🚪 ауд. 70
        Викладач
    Якщо в підгруп різні аудиторії: «Викладач · 🚪 ауд. 75».
    """
    parts = [p.strip() for p in info.split(" | ") if p.strip()]
    if not parts:
        return ""
    subject, room = split_trailing_room(parts[0])
    lines = [escape(subject or parts[0])]
    if room:
        lines.append(f"🚪 {escape(room_label(room))}")
    for p in parts[1:]:
        name, r = split_trailing_room(p)
        if r:
            door = f"🚪 {escape(room_label(r))}"
            lines.append(f"<i>{escape(name)}</i> · {door}" if name else door)
        else:
            lines.append(f"<i>{escape(p)}</i>")
    return "\n".join(lines)


async def build_schedule_message(user_id, offset=1):
    """offset=0 — сьогодні, offset=1 — завтра."""
    group = get_user_group(user_id)
    if not group:
        return "Спочатку обери групу через /start", None

    label = "Сьогодні" if offset == 0 else "Завтра"
    target = datetime.now(KYIV_TZ) + timedelta(days=offset)
    target_date = target.date()

    if target.weekday() >= 5:
        return f"{label} вихідний. Відпочивай! 🎉", get_bus_keyboard()

    day_name = DAYS_MAP[target.weekday()]

    # Базовий розклад. Якщо сайт недоступний, чесно про це кажемо.
    schedule_error = False
    try:
        base_lessons = await asyncio.to_thread(_parse_day, group, day_name)
    except Exception as e:
        print(f"Помилка розкладу для {group}: {type(e).__name__}: {e}")
        base_lessons = []
        schedule_error = True

    subs = await asyncio.to_thread(get_substitutions, group)
    subs_dates = subs.get("dates") or set()

    # Тиждень (перший/другий) береться із файлу замін і впливає на базовий розклад
    week = week_for_date(subs.get("week"), subs_dates, target_date)
    base_lessons = filter_by_week(group, day_name, base_lessons, week)
    week_note = {1: " · перший тиждень", 2: " · другий тиждень"}.get(week, "")

    # Заміни застосовуємо, лише якщо у файлі немає дат або серед них є потрібна.
    subs_apply = (not subs_dates) or (target_date in subs_dates)
    removed_pairs = subs["removed"] if subs_apply else []
    added_pairs = subs["added"] if subs_apply else {}

    final_lessons = {}

    # 1. Базові пари (якщо їх не зняли)
    for lesson in base_lessons:
        pair_num = lesson['pair']
        if str(pair_num) not in removed_pairs:
            final_lessons[pair_num] = (lesson['info'], False)

    # 2. Додаємо або замінюємо пари із замін
    for pair_str, info in added_pairs.items():
        try:
            pair_num = int(pair_str)
        except ValueError:
            continue
        if str(pair_num) not in removed_pairs and info:
            final_lessons[pair_num] = (info, True)

    notes = ""
    if schedule_error:
        notes += "⚠️ Не вдалося завантажити основний розклад із сайту, показано лише заміни.\n\n"

    note_dates = ""
    if not subs_apply:
        dates_str = ", ".join(d.strftime("%d.%m") for d in sorted(subs_dates))
        note_dates = f"\n\nℹ️ Файл замін стосується іншої дати ({dates_str}), на цей день замін немає."

    if not final_lessons:
        return f"{notes}{label} ({day_name}) пар немає (і замін теж).{note_dates}", get_bus_keyboard()

    msg = f"{notes}📅 <b>{label}, {day_name}</b>\n👥 Група {escape(group)}{week_note}\n{DIVIDER}\n\n"
    first_lesson_time = None

    for pair_num in sorted(final_lessons):
        info, is_sub = final_lessons[pair_num]
        time_str = BELL_SCHEDULE.get(pair_num, "")
        if not first_lesson_time and time_str:
            first_lesson_time = time_str.split('–')[0]
        head = f"<b>{pair_num} пара</b>"
        if time_str:
            head += f" · {time_str}"
        if is_sub:
            head += " · 🔄 <b>заміна</b>"
        msg += f"{head}\n{format_lesson(info)}\n\n"

    if first_lesson_time and offset == 1:
        msg += f"⏰ Перша пара починається о {first_lesson_time}"

    return msg.strip() + note_dates, get_bus_keyboard()


async def send_schedule(message: Message, offset=1):
    await delete_user_message(message)
    text, bus_kb = await build_schedule_message(message.from_user.id, offset)
    items = [(text, {"parse_mode": "HTML"})]
    if bus_kb:
        items.append(("Переглянути розклад автобусів?", {"reply_markup": bus_kb}))
    await show(message.chat.id, items)


async def daily_mailing():
    # Якщо розсилку цього дня вже почала інша копія бота, пропускаємо
    if not _claim("mailings", datetime.now(KYIV_TZ).strftime("%Y-%m-%d")):
        return
    users = get_all_users()
    for user_id, group in users:
        try:
            text, bus_kb = await build_schedule_message(user_id, offset=1)
            items = [(text, {"parse_mode": "HTML"})]
            if bus_kb:
                items.append(("Переглянути розклад автобусів?", {"reply_markup": bus_kb}))
            await show(user_id, items)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Не вдалося надіслати повідомлення {user_id}: {e}")


@dp.message()
async def cleanup_other_messages(message: Message):
    """Будь-який інший текст від користувача просто прибираємо з чату."""
    await delete_user_message(message)


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="today", description="Розклад на сьогодні"),
        BotCommand(command="tomorrow", description="Розклад на завтра"),
        BotCommand(command="start", description="Обрати групу"),
    ])
    scheduler.add_job(daily_mailing, 'cron', hour=18, minute=0)
    scheduler.start()
    print(f"Бот запущено! pid={os.getpid()} host={socket.gethostname()}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
