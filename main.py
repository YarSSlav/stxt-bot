import asyncio
import sqlite3
from datetime import datetime, timedelta
import pytz
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from parsers import get_groups, get_schedule_for_day, get_substitutions, get_bus_routes, get_bus_schedule, BELL_SCHEDULE


BOT_TOKEN = '9011847956:AAHFTaQuKPwg9MpAvm91AKFYFAt-hsSrNr4' 

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

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

def get_groups_keyboard():
    builder = InlineKeyboardBuilder()
    groups = get_groups()
    for group in groups:
        builder.button(text=group, callback_data=f"group_{group}")
    builder.adjust(2)
    return builder.as_markup()

def get_bus_keyboard():
    builder = InlineKeyboardBuilder()
    routes = get_bus_routes()
    for route in routes:
        builder.button(text=f"№{route}", callback_data=f"bus_{route}")
    builder.adjust(4)
    return builder.as_markup()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Выбери свою группу, чтобы я присылал расписание:", reply_markup=get_groups_keyboard())

@dp.callback_query(F.data.startswith('group_'))
async def process_group(callback: CallbackQuery):
    group_name = callback.data.split('_')[1]
    set_user_group(callback.from_user.id, group_name)
    await callback.message.edit_text(f"✅ Ты выбрал группу: {group_name}.\n\nТеперь каждый день в 18:00 я буду присылать расписание на завтра.")

@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):
    await send_schedule(message.from_user.id, message)

@dp.callback_query(F.data.startswith('bus_'))
async def process_bus(callback: CallbackQuery):
    route = callback.data.split('_')[1]
    await callback.message.answer(f"Ищу расписание для маршрута {route}...")
    schedule = get_bus_schedule(route)
    await callback.message.answer(schedule)

async def build_schedule_message(user_id):
    group = get_user_group(user_id)
    if not group:
        return "Сначала выбери группу через /start", None

    kyiv_tz = pytz.timezone('Europe/Kyiv')
    tomorrow = datetime.now(kyiv_tz) + timedelta(days=1)
    
    if tomorrow.weekday() >= 5:
        return f"Завтра выходной. Отдыхай! 🎉", get_bus_keyboard()

    days_map = {0: "Понеділок", 1: "Вівторок", 2: "Середа", 3: "Четвер", 4: "П'ятниця"}
    day_name = days_map[tomorrow.weekday()]
    
    # Получаем базовое расписание и замены
    base_lessons = get_schedule_for_day(group, day_name)
    subs = get_substitutions(group)
    removed_pairs = subs["removed"]
    added_pairs = subs["added"]
    
    final_lessons = {}
    
    # 1. Заполняем базовые пары (если их не сняли)
    for lesson in base_lessons:
        pair_num = lesson['pair']
        if str(pair_num) not in removed_pairs:
            final_lessons[pair_num] = lesson['info']
            
    # 2. Добавляем или заменяем пары из файла замен
    for pair_str, info in added_pairs.items():
        try:
            pair_num = int(pair_str)
            if str(pair_num) not in removed_pairs: # Если не сняли
                final_lessons[pair_num] = info + " *(Заміна)*"
        except ValueError:
            pass

    if not final_lessons:
        return f"Завтра ({day_name}) пар нет (и замен тоже).", get_bus_keyboard()

    msg = f"📅 *Завтра, {day_name} | Группа {group}*\n\n"
    first_lesson_time = None

    # Сортируем пары по порядку (1, 2, 3...)
    for pair_num in sorted(final_lessons.keys()):
        time = BELL_SCHEDULE.get(pair_num, "")
        if not first_lesson_time and time:
            first_lesson_time = time.split('–')[0]
            
        msg += f"*{pair_num} пара* ({time})\n{final_lessons[pair_num]}\n\n"

    if first_lesson_time:
        msg += f"⏰ Первая пара начинается в {first_lesson_time}"
    else:
        msg += "Завтра пар нет."

    return msg, get_bus_keyboard()

async def send_schedule(user_id, message: Message):
    text, bus_kb = await build_schedule_message(user_id)
    await message.answer(text, parse_mode="Markdown")
    if bus_kb:
        await message.answer("Посмотреть расписание автобусов?", reply_markup=bus_kb)

async def daily_mailing():
    users = get_all_users()
    for user_id, group in users:
        try:
            text, bus_kb = await build_schedule_message(user_id)
            await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
            if bus_kb:
                await bot.send_message(chat_id=user_id, text="Посмотреть расписание автобусов?", reply_markup=bus_kb)
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
