import asyncio
import logging
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncpg

# Конфиг
TOKEN = "8488725757:AAEiM2ek_Br93wisl4OT3ePd89JsB2GtW4"
ADMIN_ID = 5372609977
DB_URL = "postgresql://my_bot_db_9kdf_user:otNLne8N7rIB2TzkHU1GNYvHK1iPSqrV@dpg-d6cv60cr85hc73bh47o0-a/my_bot_db_9kdf"

USER_OFFSETS = {
    6809376588: -1 
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Вспомогательные функции
def get_user_now(user_id: int):
    base_now = datetime.now()
    offset = USER_OFFSETS.get(user_id, 0)
    user_now = base_now + timedelta(hours=offset)
    return user_now.timestamp()

def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours} ч. {minutes} мин."

async def get_db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    conn = await get_db_conn()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY, 
            text TEXT, 
            status INTEGER DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, 
            username TEXT, 
            accumulated_seconds INTEGER DEFAULT 0, 
            start_timestamp DOUBLE PRECISION DEFAULT NULL,
            active_task_id INTEGER DEFAULT NULL
        )
    """)
    await conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)")
    await conn.execute("INSERT INTO settings (key, value) VALUES ('daily_limit', 18000) ON CONFLICT DO NOTHING")
    
    # Проверка на колонку active_task_id (на случай если база уже была)
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN active_task_id INTEGER DEFAULT NULL")
    except:
        pass
    await conn.close()

async def get_limit():
    conn = await get_db_conn()
    row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'daily_limit'")
    await conn.close()
    return row['value'] if row else 18000

async def reset_daily_time():
    conn = await get_db_conn()
    await conn.execute("UPDATE users SET accumulated_seconds = 0, start_timestamp = NULL, active_task_id = NULL")
    await conn.close()
    logging.info("Системное уведомление: Рабочее время обнулено.")

# Хендлеры
@dp.message(Command("add", "+"))
async def add_task(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    task_text = command.args
    if not task_text:
        return await message.answer("🔘 Уведомление: Введите текст задачи в одном сообщении с командой.\nПример: /add Текст")
    
    conn = await get_db_conn()
    await conn.execute("INSERT INTO tasks (text, status) VALUES ($1, $2)", task_text, 0)
    all_users = await conn.fetch("SELECT user_id FROM users")
    await conn.close()
    
    for row in all_users:
        uid = row['user_id']
        try:
            await bot.send_message(uid, f"❗️ Новое задание: {task_text}")
        except:
            continue
            
    await message.answer(f"✅ Система: Задача добавлена и разослана:\n— {task_text}")

@dp.message(Command("clear", "-"))
async def clear_tasks(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    conn = await get_db_conn()
    await conn.execute("DELETE FROM tasks")
    await conn.execute("UPDATE users SET active_task_id = NULL")
    await conn.close()
    await message.answer("🗑 Система: Реестр задач очищен.")

@dp.message(Command("stats"))
async def show_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    conn = await get_db_conn()
    query = """
        SELECT u.user_id, u.username, u.accumulated_seconds, u.start_timestamp, t.text 
        FROM users u 
        LEFT JOIN tasks t ON u.active_task_id = t.id
    """
    users = await conn.fetch(query)
    await conn.close()
            
    if not users: return await message.answer("📊 Система: Нет данных.")
    
    res = "📊 Мониторинг персонала:\n\n"
    for row in users:
        uid, name, acc, start, task_name = row['user_id'], row['username'], row['accumulated_seconds'], row['start_timestamp'], row['text']
        now = get_user_now(uid)
        total = acc + (now - start if start else 0)
        if task_name:
            status_str = f"✏️ Выполняет: {task_name}"
        elif start:
            status_str = "🟢 В работе (без задачи)"
        else:
            status_str = "⚪️ Пауза"
        res += f"👤 {name or uid} (ID: {uid})\n— {status_str}\n— Отработано: {format_time(total)}\n\n"
    await message.answer(res)

@dp.message(Command("limit"))
async def set_limit(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if command.args and command.args.isdigit():
        conn = await get_db_conn()
        await conn.execute("UPDATE settings SET value = $1 WHERE key = $2", int(command.args)*3600, 'daily_limit')
        await conn.close()
        await message.answer(f"⚙️ Лимит изменен на {command.args} ч.")

@dp.message(Command("check"))
async def check_tasks(message: types.Message):
    conn = await get_db_conn()
    tasks = await conn.fetch("SELECT id, text, status FROM tasks")
    await conn.close()
    if not tasks: return await message.answer("📋 Статус: Список пуст.")
    
    kb = InlineKeyboardBuilder()
    icons = {0: "❌", 1: "✏️", 2: "✅"}
    for row in tasks:
        tid, txt, stat = row['id'], row['text'], row['status']
        icon = icons.get(stat, "❌")
        kb.button(text=f"{icon} {txt}", callback_data=f"cycle_{tid}_{stat}")
    kb.adjust(1)
    await message.answer("📋 Текущие задачи (нажми для смены статуса):", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("cycle_"))
async def cycle_status(call: types.CallbackQuery):
    uid = call.from_user.id
    _, tid, current_stat = call.data.split("_")
    tid = int(tid)
    new_stat = (int(current_stat) + 1) % 3
    
    conn = await get_db_conn()
    await conn.execute("UPDATE tasks SET status = $1 WHERE id = $2", new_stat, tid)
    if new_stat == 1:
        await conn.execute("UPDATE users SET active_task_id = $1 WHERE user_id = $2", tid, uid)
    else:
        await conn.execute("UPDATE users SET active_task_id = NULL WHERE user_id = $1 AND active_task_id = $2", uid, tid)
    await conn.close()
        
    await call.answer("Статус обновлен")
    await call.message.delete()
    await check_tasks(call.message)

@dp.message(Command("start"))
async def start_t(message: types.Message):
    uid = message.from_user.id
    now = get_user_now(uid)
    limit = await get_limit()
    conn = await get_db_conn()
    row = await conn.fetchrow("SELECT accumulated_seconds, start_timestamp FROM users WHERE user_id = $1", uid)
    
    acc = row['accumulated_seconds'] if row else 0
    if acc >= limit: 
        await conn.close()
        return await message.answer("🚫 Лимит времени исчерпан.")
    if row and row['start_timestamp']: 
        await conn.close()
        return await message.answer("⏳ Таймер уже запущен.")
    
    await conn.execute("""
        INSERT INTO users (user_id, username, accumulated_seconds, start_timestamp) 
        VALUES ($1, $2, $3, $4) 
        ON CONFLICT (user_id) DO UPDATE SET username = $2, start_timestamp = $4
    """, uid, message.from_user.full_name, acc, now)
    await conn.close()
    await message.answer("▶️ Система: Работа начата.")

@dp.message(Command("pause"))
async def pause_t(message: types.Message):
    uid = message.from_user.id
    now = get_user_now(uid)
    conn = await get_db_conn()
    r = await conn.fetchrow("SELECT accumulated_seconds, start_timestamp FROM users WHERE user_id = $1", uid)
    
    if r and r['start_timestamp']:
        total = r['accumulated_seconds'] + (now - r['start_timestamp'])
        await conn.execute("UPDATE users SET accumulated_seconds = $1, start_timestamp = NULL, active_task_id = NULL WHERE user_id = $2", int(total), uid)
        await conn.close()
        await message.answer(f"⏸ Пауза. Всего отработано: {format_time(total)}")
    else:
        await conn.close()

@dp.message(Command("time"))
async def show_time(message: types.Message):
    uid = message.from_user.id
    limit = await get_limit()
    conn = await get_db_conn()
    r = await conn.fetchrow("SELECT accumulated_seconds, start_timestamp FROM users WHERE user_id = $1", uid)
    await conn.close()

    if r:
        now = get_user_now(uid)
        curr = r['accumulated_seconds'] + (now - r['start_timestamp'] if r['start_timestamp'] else 0)
        local_time_str = datetime.fromtimestamp(now).strftime('%H:%M')
        msg = await message.answer(f"📊 Отчет за сегодня:\n— Отработано: {format_time(curr)}\n— Остаток: {format_time(max(0, limit-curr))}\nОбновлено: {local_time_str}")
        if r['start_timestamp']: asyncio.create_task(update_time_loop(msg, uid))
    else:
        await message.answer("📊 Отчет за сегодня:\n— Отработано: 0 ч. 0 мин.\n— Остаток: 5 ч. 0 мин.")

async def update_time_loop(message: types.Message, user_id: int):
    for _ in range(60): 
        await asyncio.sleep(60)
        limit = await get_limit()
        conn = await get_db_conn()
        row = await conn.fetchrow("SELECT accumulated_seconds, start_timestamp FROM users WHERE user_id = $1", user_id)
        await conn.close()

        if not row or not row['start_timestamp']: break
        now = get_user_now(user_id)
        current = row['accumulated_seconds'] + (now - row['start_timestamp'])
        local_time_str = datetime.fromtimestamp(now).strftime('%H:%M')
        text = (f"📊 Отчет за сегодня (Обновляемый):\n"
                f"— Отработано: {format_time(current)}\n"
                f"— Остаток: {format_time(max(0, limit-current))}\n"
                f"Обновлено: {local_time_str}")
        try: await message.edit_text(text)
        except: break

async def main():
    await init_db()
    user_cmds = [
        BotCommand(command="start", description="Старт"), 
        BotCommand(command="pause", description="Пауза"), 
        BotCommand(command="time", description="Время"), 
        BotCommand(command="check", description="Задачи")
    ]
    await bot.set_my_commands(user_cmds)
    admin_cmds = user_cmds + [
        BotCommand(command="add", description="Добавить"), 
        BotCommand(command="clear", description="Очистить"), 
        BotCommand(command="stats", description="Статистика"), 
        BotCommand(command="limit", description="Лимит")
    ]
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    scheduler.add_job(reset_daily_time, 'cron', hour=0, minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass