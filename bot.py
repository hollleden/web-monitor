import asyncio
import hashlib
import logging
import os
from datetime import datetime

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
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
    for url_id, url, title, _, is_up in rows:
        label = title if title else url.replace("https://", "").replace("http://", "")[:35]
        status = "🟢" if is_up else "🔴"
        buttons.append([
            InlineKeyboardButton(text=f"{status} {label}", url=url),
            InlineKeyboardButton(text="🗑", callback_data=f"ask_del:{url_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_delete_keyboard(url_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, remove", callback_data=f"del:{url_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_del"),
    ]])

def open_site_keyboard(url: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Open site", url=url),
    ]])

# ── Database ──────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                url       TEXT UNIQUE NOT NULL,
                title     TEXT,
                last_hash TEXT,
                is_up     INTEGER DEFAULT 1
            )
        """)
        await db.commit()

async def get_urls():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, url, title, last_hash, is_up FROM urls") as cur:
            return await cur.fetchall()

async def add_url(url: str, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO urls (url, title, is_up) VALUES (?, ?, 1)", (url, title)
        )
        await db.commit()

async def remove_url(url_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM urls WHERE id = ?", (url_id,))
        await db.commit()

async def update_hash(url_id: int, new_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE urls SET last_hash = ?, is_up = 1 WHERE id = ?", (new_hash, url_id)
        )
        await db.commit()

async def set_down(url_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE urls SET is_up = 0 WHERE id = ?", (url_id,))
        await db.commit()

# ── Fetching ──────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WebMonitor/1.0)"}

async def fetch_title(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            html = await resp.text()
            start = html.lower().find("<title>")
            end = html.lower().find("</title>")
            if start != -1 and end != -1:
                title = html[start+7:end].strip()
                return title[:50] if len(title) > 50 else title
    except:
        pass
    return url.replace("https://", "").replace("http://", "")[:40]

async def fetch_hash(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            content = await resp.text()
            return hashlib.md5(content.encode()).hexdigest()
    except Exception as e:
        logging.warning(f"Fetch error for {url}: {e}")
        return None

# ── Background checker ────────────────────────────────────────────────────────
fail_counts: dict[int, int] = {}

async def check_loop():
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            rows = await get_urls()
            for url_id, url, title, last_hash, is_up in rows:
                new_hash = await fetch_hash(session, url)
                label = title if title else url

                if new_hash is None:
                    fail_counts[url_id] = fail_counts.get(url_id, 0) + 1
                    if fail_counts[url_id] == 3 and is_up:
                        await set_down(url_id)
                        now = datetime.now().strftime("%d %b %Y, %H:%M")
                        await bot.send_message(
                            ALLOWED_ID,
                            f"🔴 <b>Site is down!</b>\n\n"
                            f"📄 {label}\n"
                            f"🔗 {url}\n\n"
                            f"🕐 {now}",
                            parse_mode="HTML",
                            reply_markup=open_site_keyboard(url),
                        )
                    continue

                fail_counts[url_id] = 0

                if last_hash is None:
                    await update_hash(url_id, new_hash)
                elif new_hash != last_hash:
                    await update_hash(url_id, new_hash)
                    now = datetime.now().strftime("%d %b %Y, %H:%M")
                    await bot.send_message(
                        ALLOWED_ID,
                        f"🔔 <b>Change detected!</b>\n\n"
                        f"📄 {label}\n"
                        f"🔗 {url}\n\n"
                        f"🕐 {now}",
                        parse_mode="HTML",
                        reply_markup=open_site_keyboard(url),
                    )

            await asyncio.sleep(CHECK_EVERY)

# ── Auth ──────────────────────────────────────────────────────────────────────
def only_me(message: types.Message) -> bool:
    return message.from_user.id == ALLOWED_ID

# ── Handlers ──────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not only_me(message): return
    # Uncomment and replace URL to show a welcome image:
    # await message.answer_photo(
    #     photo="https://i.imgur.com/yourimage.jpg",
    #     caption="🌿 <b>Mzekali is watching.</b>...",
    #     parse_mode="HTML",
    #     reply_markup=main_keyboard(),
    # )
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
        "🟢 / 🔴 — site is up or down\n"
        "🗑 — remove a site (asks for confirmation)\n\n"
        "I'll ping you the moment something changes 🔔\n"
        "I'll also alert you if a site goes down 🔴",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "➕ Add website")
async def ask_url(message: types.Message):
    if not only_me(message): return
    await message.answer(
        "🔗 Just send me any URL and I'll start monitoring it.\n\n"
        "<i>Example: https://example.com</i>",
        parse_mode="HTML",
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
    up = sum(1 for r in rows if r[4])
    down = len(rows) - up
    summary = f"🟢 {up} up" + (f"  🔴 {down} down" if down else "")
    await message.answer(
        f"📋 <b>Monitoring {len(rows)} site(s):</b>  {summary}",
        parse_mode="HTML",
        reply_markup=list_keyboard(rows),
    )

@dp.message(F.text.startswith("http"))
async def auto_add_url(message: types.Message):
    if not only_me(message): return
    url = message.text.strip()
    status_msg = await message.answer("🔍 Checking site...")
    async with aiohttp.ClientSession() as session:
        title = await fetch_title(session, url)
    await add_url(url, title)
    await status_msg.edit_text(
        f"✅ <b>Added!</b>\n\n"
        f"📄 {title}\n"
        f"🔗 {url}\n\n"
        f"Checking every {CHECK_EVERY // 60} min.",
        parse_mode="HTML",
    )

@dp.callback_query(F.data.startswith("ask_del:"))
async def ask_delete(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    url_id = int(callback.data.split(":")[1])
    await callback.message.answer(
        "🗑 Are you sure you want to remove this site?",
        reply_markup=confirm_delete_keyboard(url_id),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("del:"))
async def delete_url(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    url_id = int(callback.data.split(":")[1])
    await remove_url(url_id)
    await callback.message.edit_text("🗑 Removed.")
    await callback.answer("Done")

@dp.callback_query(F.data == "cancel_del")
async def cancel_delete(callback: types.CallbackQuery):
    await callback.message.edit_text("👌 Cancelled.")
    await callback.answer()

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(check_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
