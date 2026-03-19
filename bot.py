import asyncio
import hashlib
import logging
import os

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ALLOWED_ID  = int(os.environ["ALLOWED_ID"])
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "300"))
DB_PATH     = "monitor.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ── FSM ───────────────────────────────────────────────────────────────────────
class AddURL(StatesGroup):
    waiting_for_url = State()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Add website")],
            [KeyboardButton(text="📋 My list"), KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
    )

def list_keyboard(rows):
    buttons = []
    for url_id, url, _ in rows:
        short = url.replace("https://", "").replace("http://", "")
        if len(short) > 35:
            short = short[:35] + "…"
        buttons.append([
            InlineKeyboardButton(text=f"🔗 {short}", url=url),
            InlineKeyboardButton(text="🗑 Remove", callback_data=f"del:{url_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ── Database ──────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                url       TEXT UNIQUE NOT NULL,
                last_hash TEXT
            )
        """)
        await db.commit()

async def get_urls():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, url, last_hash FROM urls") as cur:
            return await cur.fetchall()

async def add_url(url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO urls (url) VALUES (?)", (url,))
        await db.commit()

async def remove_url(url_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM urls WHERE id = ?", (url_id,))
        await db.commit()

async def update_hash(url_id: int, new_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE urls SET last_hash = ? WHERE id = ?", (new_hash, url_id))
        await db.commit()

# ── Fetching ──────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WebMonitor/1.0)"}

async def fetch_hash(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            content = await resp.text()
            return hashlib.md5(content.encode()).hexdigest()
    except Exception as e:
        logging.warning(f"Fetch error for {url}: {e}")
        return None

# ── Background checker ────────────────────────────────────────────────────────
async def check_loop():
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            rows = await get_urls()
            for url_id, url, last_hash in rows:
                new_hash = await fetch_hash(session, url)
                if new_hash is None:
                    continue
                if last_hash is None:
                    await update_hash(url_id, new_hash)
                elif new_hash != last_hash:
                    await update_hash(url_id, new_hash)
                    await bot.send_message(
                        ALLOWED_ID,
                        f"🔔 <b>Change detected!</b>\n\n🔗 {url}",
                        parse_mode="HTML"
                    )
            await asyncio.sleep(CHECK_EVERY)

# ── Auth ──────────────────────────────────────────────────────────────────────
def only_me(message: types.Message) -> bool:
    return message.from_user.id == ALLOWED_ID

# ── Handlers ──────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not only_me(message): return
    await message.answer(
        "🌿 <b>Mzekali is watching.</b>\n\n"
        "Named after the Georgian goddess of the forest — "
        "I see everything that moves on the web.\n\n"
        "Add a site and I'll alert you the moment something changes.\n\n"
        f"⏱ Checks every <b>{CHECK_EVERY // 60} min</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "ℹ️ Help")
async def cmd_help(message: types.Message):
    if not only_me(message): return
    await message.answer(
        "📖 <b>How to use:</b>\n\n"
        "➕ <b>Add website</b> — start monitoring a URL\n"
        "📋 <b>My list</b> — view all monitored sites\n"
        "🗑 <b>Remove</b> — stop monitoring a site\n\n"
        "I'll ping you the moment something changes 🔔",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "➕ Add website")
async def ask_url(message: types.Message, state: FSMContext):
    if not only_me(message): return
    await state.set_state(AddURL.waiting_for_url)
    await message.answer(
        "🔗 Send me the URL to monitor:\n\n<i>Example: https://example.com</i>",
        parse_mode="HTML",
        reply_markup=types.ReplyKeyboardRemove(),
    )

@dp.message(AddURL.waiting_for_url)
async def receive_url(message: types.Message, state: FSMContext):
    if not only_me(message): return
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("❌ That doesn't look like a URL. Try again:")
        return
    await add_url(url)
    await state.clear()
    await message.answer(
        f"✅ <b>Added!</b>\n\n🔗 {url}\n\nChecking every {CHECK_EVERY // 60} min.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "📋 My list")
async def cmd_list(message: types.Message):
    if not only_me(message): return
    rows = await get_urls()
    if not rows:
        await message.answer(
            "📭 Your list is empty.\n\nTap <b>➕ Add website</b> to get started.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return
    await message.answer(
        f"📋 <b>Monitoring {len(rows)} site(s):</b>\n\nTap 🗑 Remove to stop monitoring",
        parse_mode="HTML",
        reply_markup=list_keyboard(rows),
    )

@dp.callback_query(F.data.startswith("del:"))
async def delete_url(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    url_id = int(callback.data.split(":")[1])
    await remove_url(url_id)
    rows = await get_urls()
    if not rows:
        await callback.message.edit_text("📭 Your list is empty.")
    else:
        await callback.message.edit_reply_markup(reply_markup=list_keyboard(rows))
    await callback.answer("🗑 Removed")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(check_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
