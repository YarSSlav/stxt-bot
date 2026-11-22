import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from html import escape

import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from parsers import (
    BELL_SCHEDULE,
    SCHEDULE_URL,
    TTL_SCHEDULE,
    _group_lines,
    _parse_day,
    _soup,
    get_bus_routes,
    get_bus_schedule,
    get_groups,
    get_substitutions,
)

# Токен береться зі змінної середовища BOT_TOKEN (не зберігай його в коді!)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задано змінну середовища BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

DAYS_MAP = {0: "Понеділок", 1: "Вівторок", 2: "Середа", 3: "Четвер", 4: "П'ятниця"}


# ───────────────────────── База даних ─────────────────────────

def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        group_name TEXT
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


# ───────────────────────── Клавіатури ─────────────────────────

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


# ───────────────────────── Команди ─────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Выбери свою группу, чтобы я присылал расписание:",
        reply_markup=await get_groups_keyboard(),
    )


@dp.callback_query(F.data.startswith('group_'))
async def process_group(callback: CallbackQuery):
    group_name = callback.data.split('_', 1)[1]
    set_user_group(callback.from_user.id, group_name)
    await callback.message.edit_text(
        f"✅ Ты выбрал группу: {group_name}.\n\n"
        f"Теперь каждый день в 18:00 я буду присылать расписание на завтра."
    )


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):
    await send_schedule(message.from_user.id, message)


@dp.callback_query(F.data.startswith('bus_'))
async def process_bus(callback: CallbackQuery):
    route = callback.data.split('_', 1)[1]
    await callback.answer()
    await callback.message.answer(f"Ищу расписание для маршрута {route}...")
    schedule = await asyncio.to_thread(get_bus_schedule, route)
    await callback.message.answer(schedule)

import socket

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.answer(f"pid={os.getpid()} host={socket.gethostname()}")
    
@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    """Показує, що парсер реально бачить на сайті для твоєї групи."""
    group = get_user_group(message.from_user.id)
    if not group:
        await message.answer("Сначала выбери группу через /start")
        return

    def work():
        return _group_lines(_soup(SCHEDULE_URL, TTL_SCHEDULE), group)

    try:
        lines = await asyncio.to_thread(work)
        text = f"Группа: {group}\nСтрок в блоке группы: {len(lines)}\n\n" + "\n".join(lines[:50])
    except Exception as e:
        text = f"Ошибка: {type(e).__name__}: {e}"
    await message.answer(text[:4000])


# ───────────────────────── Збірка повідомлення ─────────────────────────

async def build_schedule_message(user_id):
    group = get_user_group(user_id)
    if not group:
        return "Сначала выбери группу через /start", None

    kyiv_tz = pytz.timezone('Europe/Kyiv')
    tomorrow = datetime.now(kyiv_tz) + timedelta(days=1)

    if tomorrow.weekday() >= 5:
        return "Завтра выходной. Отдыхай! 🎉", get_bus_keyboard()

    day_name = DAYS_MAP[tomorrow.weekday()]

    # Базовий розклад. Якщо сайт недоступний, чесно про це кажемо.
    schedule_error = False
    try:
        base_lessons = await asyncio.to_thread(_parse_day, group, day_name)
    except Exception as e:
        print(f"Помилка розкладу для {group}: {type(e).__name__}: {e}")
        base_lessons = []
        schedule_error = True

    subs = await asyncio.to_thread(get_substitutions, group)
    removed_pairs = subs["removed"]
    added_pairs = subs["added"]

    final_lessons = {}
    changed = set()

    # 1. Базові пари (якщо їх не зняли)
    for lesson in base_lessons:
        pair_num = lesson['pair']
        if str(pair_num) not in removed_pairs:
            final_lessons[pair_num] = escape(lesson['info'])

    # 2. Додаємо або замінюємо пари із замін
    for pair_str, info in added_pairs.items():
        try:
            pair_num = int(pair_str)
        except ValueError:
            continue
        if str(pair_num) not in removed_pairs and info:
            final_lessons[pair_num] = f"{escape(info)} <b>(Заміна)</b>"
            changed.add(pair_num)

    warning = ""
    if schedule_error:
        warning = "⚠️ Не удалось загрузить основное расписание с сайта, показаны только замены.\n\n"

    if not final_lessons:
        return f"{warning}Завтра ({day_name}) пар нет (и замен тоже).", get_bus_keyboard()

    msg = f"{warning}📅 <b>Завтра, {day_name} | Группа {escape(group)}</b>\n\n"
    first_lesson_time = None

    for pair_num in sorted(final_lessons):
        time_str = BELL_SCHEDULE.get(pair_num, "")
        if not first_lesson_time and time_str:
            first_lesson_time = time_str.split('–')[0]
        time_part = f" ({time_str})" if time_str else ""
        msg += f"<b>{pair_num} пара</b>{time_part}\n{final_lessons[pair_num]}\n\n"

    if first_lesson_time:
        msg += f"⏰ Первая пара начинается в {first_lesson_time}"

    return msg.strip(), get_bus_keyboard()


async def send_schedule(user_id, message: Message):
    text, bus_kb = await build_schedule_message(user_id)
    await message.answer(text, parse_mode="HTML")
    if bus_kb:
        await message.answer("Посмотреть расписание автобусов?", reply_markup=bus_kb)


async def daily_mailing():
    users = get_all_users()
    for user_id, group in users:
        try:
            text, bus_kb = await build_schedule_message(user_id)
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            if bus_kb:
                await bot.send_message(
                    chat_id=user_id,
                    text="Посмотреть расписание автобусов?",
                    reply_markup=bus_kb,
                )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Не удалось отправить сообщение {user_id}: {e}")


async def main():
    init_db()
    scheduler.add_job(daily_mailing, 'cron', hour=18, minute=0)
    scheduler.start()
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
