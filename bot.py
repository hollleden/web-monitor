import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone, timedelta

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
DB_PATH     = os.getenv("DB_PATH", "monitor.db")  # set to /app/data/monitor.db with Railway Volume
TZ_OFFSET   = int(os.getenv("TZ_OFFSET", "0"))    # e.g. 4 for Tbilisi (GMT+4)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

def now_str() -> str:
    tz = timezone(timedelta(hours=TZ_OFFSET))
    return datetime.now(tz).strftime("%d %b %Y, %H:%M")

def normalize_url(url: str) -> str:
    """Strip trailing slash, extract first word, ensure https."""
    url = url.split()[0].strip()  # take first word only — fix #4
    if url.startswith("http://"):
        url = "https://" + url[7:]  # normalize http→https — fix #3
    return url.rstrip("/")  # strip trailing slash — fix #3

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 My list"), KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
    )

def list_keyboard(rows):
    buttons = []
    for url_id, url, title, _, is_up in rows:
        label = title if title else url.replace("https://", "")[:35]
        status = "🟢" if is_up else "🔴"
        # Store url hash in callback to avoid stale id issue — fix #10
        url_key = hashlib.md5(url.encode()).hexdigest()[:12]
        buttons.append([
            InlineKeyboardButton(text=f"{status} {label}", url=url),
            InlineKeyboardButton(text="🗑", callback_data=f"ask_del:{url_key}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_delete_keyboard(url_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, remove", callback_data=f"del:{url_key}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_del"),
    ]])

def open_site_keyboard(url: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Open site", url=url),
    ]])

# ── Database ──────────────────────────────────────────────────────────────────
async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
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

async def url_exists(url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM urls WHERE url = ?", (url,)) as cur:
            return await cur.fetchone() is not None

async def add_url(url: str, title: str, first_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO urls (url, title, last_hash, is_up) VALUES (?, ?, ?, 1)",
            (url, title, first_hash)  # save hash on add — fix #7
        )
        await db.commit()

async def remove_url_by_key(url_key: str):
    """Remove by url hash key — fix #10."""
    rows = await get_urls()
    for url_id, url, *_ in rows:
        if hashlib.md5(url.encode()).hexdigest()[:12] == url_key:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM urls WHERE id = ?", (url_id,))
                await db.commit()
            return url

async def update_hash(url_id: int, new_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE urls SET last_hash = ?, is_up = 1 WHERE id = ?", (new_hash, url_id)
        )
        await db.commit()

async def set_status(url_id: int, is_up: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE urls SET is_up = ? WHERE id = ?", (1 if is_up else 0, url_id))
        await db.commit()

# ── Fetching ──────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WebMonitor/1.0)"}

async def fetch_page(session: aiohttp.ClientSession, url: str):
    """Fetch page once, return (title, hash) — fix #7."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            html = await resp.text()
            # extract title
            start = html.lower().find("<title>")
            end = html.lower().find("</title>")
            title = None
            if start != -1 and end != -1:
                t = html[start+7:end].strip()
                title = t[:50] if len(t) > 50 else t
            if not title:
                title = url.replace("https://", "")[:40]
            page_hash = hashlib.md5(html.encode()).hexdigest()
            return title, page_hash
    except Exception as e:
        logging.warning(f"Fetch error for {url}: {e}")
        return None, None

# ── Background checker ────────────────────────────────────────────────────────
fail_counts: dict[int, int] = {}

async def check_loop():
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                rows = await get_urls()
                for url_id, url, title, last_hash, is_up in rows:
                    try:  # fix #5 — isolate each site
                        _, new_hash = await fetch_page(session, url)
                        label = title if title else url

                        if new_hash is None:
                            fail_counts[url_id] = fail_counts.get(url_id, 0) + 1
                            if fail_counts[url_id] == 3 and is_up:
                                await set_status(url_id, False)
                                await bot.send_message(
                                    ALLOWED_ID,
                                    f"🔴 <b>Site is down!</b>\n\n"
                                    f"📄 {label}\n🔗 {url}\n\n🕐 {now_str()}",
                                    parse_mode="HTML",
                                    reply_markup=open_site_keyboard(url),
                                )
                            continue

                        # site is up — check if it just recovered — fix #1
                        if not is_up:
                            await set_status(url_id, True)
                            await bot.send_message(
                                ALLOWED_ID,
                                f"🟢 <b>Site is back up!</b>\n\n"
                                f"📄 {label}\n🔗 {url}\n\n🕐 {now_str()}",
                                parse_mode="HTML",
                                reply_markup=open_site_keyboard(url),
                            )

                        # fix #6 — reset fail count on success
                        fail_counts[url_id] = 0

                        if last_hash is None:
                            await update_hash(url_id, new_hash)
                        elif new_hash != last_hash:
                            await update_hash(url_id, new_hash)
                            await bot.send_message(
                                ALLOWED_ID,
                                f"🔔 <b>Change detected!</b>\n\n"
                                f"📄 {label}\n🔗 {url}\n\n🕐 {now_str()}",
                                parse_mode="HTML",
                                reply_markup=open_site_keyboard(url),
                            )
                    except Exception as e:
                        logging.error(f"Error processing {url}: {e}")  # fix #5

            except Exception as e:
                logging.error(f"check_loop error: {e}")  # fix #5

            await asyncio.sleep(CHECK_EVERY)

# ── Auth ──────────────────────────────────────────────────────────────────────
def only_me(message: types.Message) -> bool:
    return message.from_user.id == ALLOWED_ID

# ── Handlers ──────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not only_me(message): return
    # To show a welcome image, uncomment and replace with your image URL:
    # await message.answer_photo(
    #     photo="https://i.imgur.com/yourimage.jpg",
    #     caption="🌿 <b>Mzekali is watching.</b>",
    #     parse_mode="HTML",
    #     reply_markup=main_keyboard(),
    # )
    await message.answer(
        "🌿 <b>Mzekali is watching.</b>\n\n"
        "Named after the Georgian goddess of the forest — "
        "I see everything that moves on the web.\n\n"
        "Just send me any URL and I'll start monitoring it.\n\n"
        f"⏱ Checks every <b>{CHECK_EVERY // 60} min</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "ℹ️ Help")
async def cmd_help(message: types.Message):
    if not only_me(message): return
    await message.answer(
        "📖 <b>How to use:</b>\n\n"
        "🔗 <b>Send any URL</b> — I'll start monitoring it\n"
        "📋 <b>My list</b> — view all monitored sites\n"
        "🟢 site is up  /  🔴 site is down\n"
        "🗑 — remove a site (asks for confirmation)\n\n"
        "I'll ping you when something changes 🔔\n"
        "I'll alert you if a site goes down 🔴 or comes back 🟢",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "📋 My list")
async def cmd_list(message: types.Message):
    if not only_me(message): return
    rows = await get_urls()
    if not rows:
        await message.answer(
            "📭 Your list is empty.\n\nJust send me any URL to get started.",
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
    url = normalize_url(message.text)  # fix #3 #4

    if await url_exists(url):
        await message.answer(f"👀 Already monitoring this one.\n\n🔗 {url}")
        return

    status_msg = await message.answer("🔍 Checking site...")
    async with aiohttp.ClientSession() as session:
        title, first_hash = await fetch_page(session, url)  # one request — fix #7

    if first_hash is None:
        await status_msg.edit_text(
            f"❌ <b>Could not reach this site.</b>\n\n🔗 {url}\n\n"
            "Check the URL and try again.",
            parse_mode="HTML",
        )
        return

    await add_url(url, title, first_hash)
    await status_msg.edit_text(
        f"✅ <b>Added!</b>\n\n"
        f"📄 {title}\n"
        f"🔗 {url}\n\n"
        f"Checking every {CHECK_EVERY // 60} min.",
        parse_mode="HTML",
    )

@dp.message(F.text)
async def unknown_text(message: types.Message):
    if not only_me(message): return
    await message.answer(  # fix #9
        "🔗 Send me a URL to monitor it.\n\n<i>Example: https://example.com</i>",
        parse_mode="HTML",
    )

@dp.callback_query(F.data.startswith("ask_del:"))
async def ask_delete(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    url_key = callback.data.split(":")[1]
    await callback.message.answer(
        "🗑 Are you sure you want to remove this site?",
        reply_markup=confirm_delete_keyboard(url_key),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("del:"))
async def delete_url(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    url_key = callback.data.split(":")[1]
    removed_url = await remove_url_by_key(url_key)
    # fix #6 — clean up fail_counts for removed url
    rows = await get_urls()
    valid_ids = {r[0] for r in rows}
    for k in list(fail_counts.keys()):
        if k not in valid_ids:
            del fail_counts[k]
    await callback.message.edit_text(
        f"🗑 Removed.\n\n🔗 {removed_url}" if removed_url else "🗑 Removed."
    )
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
