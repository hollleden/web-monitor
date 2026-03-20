import asyncio
import difflib
import hashlib
import logging
import os
import re
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
DB_PATH     = os.getenv("DB_PATH", "monitor.db")
TZ_OFFSET   = int(os.getenv("TZ_OFFSET", "0"))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

def now_str() -> str:
    tz = timezone(timedelta(hours=TZ_OFFSET))
    return datetime.now(tz).strftime("%d %b %Y, %H:%M")

def extract_domain(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").split("/")[0]

def normalize_url(url: str) -> str:
    url = url.split()[0].strip()
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url.rstrip("/")

def html_to_text(html: str) -> list[str]:
    """Strip HTML tags, scripts, styles — return clean lines of text."""
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.S)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    lines = [l.strip() for l in text.split(".") if len(l.strip()) > 20]
    return lines

def build_diff(old_text: list[str], new_text: list[str]) -> str:
    old_set = set(old_text)
    new_set = set(new_text)
    added   = [l for l in new_text if l not in old_set and len(l) > 40]
    removed = [l for l in old_text if l not in new_set and len(l) > 40]

    # too many changes = dynamic page, skip diff
    if len(added) + len(removed) > 20:
        return ""

    parts = []
    for line in added[:4]:
        parts.append(f"➕ {line[:120]}")
    for line in removed[:4]:
        parts.append(f"➖ {line[:120]}")

    return "\n".join(parts)

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 My list"), KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
    )

def site_keyboard(url_key: str, url: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Open", url=url),
        InlineKeyboardButton(text="🗑 Remove", callback_data=f"ask_del:{url_key}"),
    ]])

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
    if os.path.dirname(DB_PATH):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                url       TEXT UNIQUE NOT NULL,
                title     TEXT,
                last_hash TEXT,
                last_text TEXT,
                is_up     INTEGER DEFAULT 1
            )
        """)
        await db.commit()

async def get_urls():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, url, title, last_hash, last_text, is_up FROM urls") as cur:
            return await cur.fetchall()

async def url_exists(url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM urls WHERE url = ?", (url,)) as cur:
            return await cur.fetchone() is not None

async def add_url(url: str, title: str, first_hash: str, first_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO urls (url, title, last_hash, last_text, is_up) VALUES (?, ?, ?, ?, 1)",
            (url, title, first_hash, first_text)
        )
        await db.commit()

async def remove_url_by_key(url_key: str):
    rows = await get_urls()
    for url_id, url, *_ in rows:
        if hashlib.md5(url.encode()).hexdigest()[:12] == url_key:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM urls WHERE id = ?", (url_id,))
                await db.commit()
            return url

async def update_page(url_id: int, new_hash: str, new_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE urls SET last_hash = ?, last_text = ?, is_up = 1 WHERE id = ?",
            (new_hash, new_text, url_id)
        )
        await db.commit()

async def set_status(url_id: int, is_up: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE urls SET is_up = ? WHERE id = ?", (1 if is_up else 0, url_id))
        await db.commit()

# ── Fetching ──────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WebMonitor/1.0)"}

async def fetch_page(session: aiohttp.ClientSession, url: str):
    """Returns (title, hash, text_lines) or (None, None, None) on error."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            html = await resp.text()
            # title
            start = html.lower().find("<title>")
            end   = html.lower().find("</title>")
            title = None
            if start != -1 and end != -1:
                t = html[start+7:end].strip()
                title = t[:50] if len(t) > 50 else t
            if not title:
                title = url.replace("https://", "")[:40]
            page_hash = hashlib.md5(html.encode()).hexdigest()
            text_lines = html_to_text(html)
            return title, page_hash, text_lines
    except Exception as e:
        logging.warning(f"Fetch error for {url}: {e}")
        return None, None, None

# ── Background checker ────────────────────────────────────────────────────────
fail_counts: dict[int, int] = {}
last_notified: dict[int, float] = {}
COOLDOWN = int(os.getenv("COOLDOWN", "3600"))  # seconds, default 1 hour

async def check_loop():
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                rows = await get_urls()
                for url_id, url, title, last_hash, last_text, is_up in rows:
                    try:
                        label = title if title else url
                        _, new_hash, new_text = await fetch_page(session, url)

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

                        if not is_up:
                            await set_status(url_id, True)
                            await bot.send_message(
                                ALLOWED_ID,
                                f"🟢 <b>Site is back up!</b>\n\n"
                                f"📄 {label}\n🔗 {url}\n\n🕐 {now_str()}",
                                parse_mode="HTML",
                                reply_markup=open_site_keyboard(url),
                            )

                        fail_counts[url_id] = 0

                        if last_hash is None:
                            await update_page(url_id, new_hash, "\n".join(new_text))
                        elif new_hash != last_hash:
                            import time
                            now_ts = time.time()
                            old_text = last_text.split("\n") if last_text else []
                            diff = build_diff(old_text, new_text)
                            await update_page(url_id, new_hash, "\n".join(new_text))

                            # cooldown — don't spam if site changes constantly
                            if now_ts - last_notified.get(url_id, 0) < COOLDOWN:
                                continue
                            last_notified[url_id] = now_ts

                            msg = (
                                f"🔔 <b>Change detected!</b>\n\n"
                                f"📄 {label}\n"
                                f"🔗 {url}\n"
                                f"🕐 {now_str()}"
                            )
                            if diff:
                                msg += f"\n\n<blockquote>{diff}</blockquote>"

                            await bot.send_message(
                                ALLOWED_ID, msg,
                                parse_mode="HTML",
                                reply_markup=open_site_keyboard(url),
                            )
                    except Exception as e:
                        logging.error(f"Error processing {url}: {e}")
            except Exception as e:
                logging.error(f"check_loop error: {e}")

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
        "I'll show you what exactly changed ➕➖\n"
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
    up = sum(1 for r in rows if r[5])
    down = len(rows) - up
    summary = f"🟢 {up} up" + (f"  🔴 {down} down" if down else "")
    await message.answer(
        f"📋 <b>Monitoring {len(rows)} site(s):</b>  {summary}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
    for _, url, title, __, ___, is_up in rows:
        status = "🟢" if is_up else "🔴"
        label = title if title else url
        domain = extract_domain(url)
        url_key = hashlib.md5(url.encode()).hexdigest()[:12]
        await message.answer(
            f"{status} <b>{label}</b>\n<i>{domain}</i>",
            parse_mode="HTML",
            reply_markup=site_keyboard(url_key, url),
        )

@dp.message(F.text.startswith("http"))
async def auto_add_url(message: types.Message):
    if not only_me(message): return
    url = normalize_url(message.text)

    if await url_exists(url):
        await message.answer(f"👀 Already monitoring this one.\n\n🔗 {url}")
        return

    status_msg = await message.answer("🔍 Checking site...")
    async with aiohttp.ClientSession() as session:
        title, first_hash, first_text = await fetch_page(session, url)

    if first_hash is None:
        await status_msg.edit_text(
            f"❌ <b>Could not reach this site.</b>\n\n🔗 {url}\n\nCheck the URL and try again.",
            parse_mode="HTML",
        )
        return

    await add_url(url, title, first_hash, "\n".join(first_text))
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
    await message.answer(
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
