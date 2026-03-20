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
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "3600"))
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
            [KeyboardButton(text="🔄 Check now")],
        ],
        resize_keyboard=True,
    )

def list_view_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Manage", callback_data="manage"),
        InlineKeyboardButton(text="🗑 Delete all", callback_data="ask_delete_all"),
    ]])

def manage_keyboard(rows):
    buttons = []
    row = []
    for i, (url_id, url, title, _, __, is_up) in enumerate(rows, 1):
        label = (title or extract_domain(url))[:12]
        row.append(InlineKeyboardButton(text=f"{i}. {label}", callback_data=f"pick:{i-1}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_manage")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_delete_keyboard(url_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, remove", callback_data=f"del:{url_key}"),
        InlineKeyboardButton(text="❌ No", callback_data="cancel_manage"),
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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

async def smart_title(session: aiohttp.ClientSession, raw_title: str, domain: str) -> str:
    """Ask Claude to shorten the page title to 2-4 words."""
    if not ANTHROPIC_API_KEY:
        return raw_title
    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 20,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Create a short 2-4 word label for this webpage. "
                        f"Format: [Company] [Page type]. Examples: 'Tripledot Jobs', 'Coursera Careers', 'Playkot Hiring'. "
                        f"Use the company name from domain if title is unclear. "
                        f"Return ONLY the label, nothing else.\n\n"
                        f"Domain: {domain}\n"
                        f"Title: {raw_title}"
                    )
                }]
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            short = data["content"][0]["text"].strip().strip('"')
            return short if short else raw_title
    except Exception as e:
        logging.warning(f"smart_title error: {e}")
        return raw_title

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
            import time
            try:
                rows = await get_urls()
                changes = []   # collect all changes this cycle
                downs   = []   # collect all new downtime alerts
                ups     = []   # collect all recovery alerts

                for url_id, url, title, last_hash, last_text, is_up in rows:
                    try:
                        label = title if title else extract_domain(url)
                        _, new_hash, new_text = await fetch_page(session, url)

                        if new_hash is None:
                            fail_counts[url_id] = fail_counts.get(url_id, 0) + 1
                            if fail_counts[url_id] == 3 and is_up:
                                await set_status(url_id, False)
                                downs.append(f"🔴 <b>{label}</b>  <i>{extract_domain(url)}</i>")
                            continue

                        if not is_up:
                            await set_status(url_id, True)
                            ups.append(f"🟢 <b>{label}</b>  <i>{extract_domain(url)}</i>")

                        fail_counts[url_id] = 0

                        if last_hash is None:
                            await update_page(url_id, new_hash, "\n".join(new_text))
                        elif new_hash != last_hash:
                            now_ts = time.time()
                            old_text = last_text.split("\n") if last_text else []
                            diff = build_diff(old_text, new_text)
                            await update_page(url_id, new_hash, "\n".join(new_text))

                            if now_ts - last_notified.get(url_id, 0) < COOLDOWN:
                                continue
                            last_notified[url_id] = now_ts

                            entry = f"<b>{label}</b>  <i>{extract_domain(url)}</i>"
                            if diff:
                                entry += f"\n{diff}"
                            changes.append((entry, url))

                    except Exception as e:
                        logging.error(f"Error processing {url}: {e}")

                # send one batched message for changes
                if changes:
                    if len(changes) == 1:
                        entry, url = changes[0]
                        msg = f"✨ update from mzekali\n\n{entry}\n\n· {now_str()} ·"
                        await bot.send_message(
                            ALLOWED_ID, msg,
                            parse_mode="HTML",
                            reply_markup=open_site_keyboard(url),
                            disable_web_page_preview=True,
                        )
                    else:
                        lines = "\n\n".join(f"• {e}" for e, _ in changes)
                        msg = f"✨ {len(changes)} updates\n\n{lines}\n\n· {now_str()} ·"
                        await bot.send_message(
                            ALLOWED_ID, msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )

                # send downs
                if downs:
                    msg = "🚫 can't reach:\n\n" + "\n".join(downs) + f"\n\n· {now_str()} ·"
                    await bot.send_message(ALLOWED_ID, msg, parse_mode="HTML", disable_web_page_preview=True)

                # send recoveries
                if ups:
                    msg = "✅ back online:\n\n" + "\n".join(ups) + f"\n\n· {now_str()} ·"
                    await bot.send_message(ALLOWED_ID, msg, parse_mode="HTML", disable_web_page_preview=True)

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
        "🌙 <b>mzekali’s here.</b>\n\n"
        "named after a georgian forest goddess, but basically i just watch websites for you.\n\n"
        "send me a url and i’ll start monitoring.\n\n"
        f"⏱ i check every {CHECK_EVERY // 3600} hr" if CHECK_EVERY >= 3600 else f"⏱ i check every {CHECK_EVERY // 60} min",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "ℹ️ Help")
async def cmd_help(message: types.Message):
    if not only_me(message): return
    await message.answer(
        "💡 <b>quick guide:</b>\n\n"
        "🔗 send any url → i’ll watch it\n"
        "📋 <b>my list</b> → see what i’m tracking\n"
        "🔄 <b>check now</b> → scan right now\n"
        "🗑 <b>manage</b> → remove sites\n\n"
        "🟢 up / 🔴 down\n\n"
        "i’ll tell you if something changes, dies, or wakes up.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "📋 My list")
async def cmd_list(message: types.Message):
    if not only_me(message): return
    rows = await get_urls()
    if not rows:
        await message.answer(
            "📭 list is empty.\n\nsend me a url and i’ll keep an eye on it.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return
    up = sum(1 for r in rows if r[5])
    down = len(rows) - up
    summary = f"🟢 {up} up" + (f"  🔴 {down} down" if down else "")
    lines = "\n\n".join(
        f"{'🟢' if r[5] else '🔴'} <b>{i}. <a href=\"{r[1]}\">{(r[2] or extract_domain(r[1]))[:35]}</a></b>"
        + (f"\n    <code>{extract_domain(r[1])}</code>" if r[2] else "")
        for i, r in enumerate(rows, 1)
    )
    await message.answer(
        f"📋 <b>{len(rows)} site(s) in my list</b>  {summary}\n\n{lines}",
        parse_mode="HTML",
        reply_markup=list_view_keyboard(),
        disable_web_page_preview=True,
    )

@dp.message(F.text.startswith("http"))
async def auto_add_url(message: types.Message):
    if not only_me(message): return
    url = normalize_url(message.text)

    if await url_exists(url):
        await message.answer(f"👀 i’m already tracking that.\n\n🔗 {url}")
        return

    status_msg = await message.answer("🔍 checking site...")
    async with aiohttp.ClientSession() as session:
        title, first_hash, first_text = await fetch_page(session, url)

    if first_hash is None:
        await status_msg.edit_text(
            f"❌ couldn’t reach it.\n\n🔗 {url}\n\ncheck the url and try again.",
            parse_mode="HTML",
        )
        return

    # smart rename via Claude
    async with aiohttp.ClientSession() as session:
        short_title = await smart_title(session, title, extract_domain(url))

    await add_url(url, short_title, first_hash, "\n".join(first_text))
    await status_msg.edit_text(
        f"✅ <b>got it.</b>\n\n"
        f"📄 {short_title}\n"
        f"<code>{extract_domain(url)}</code>",
        parse_mode="HTML",
    )

last_manual_check: float = 0
CHECK_NOW_COOLDOWN = 300  # 5 minutes

@dp.message(F.text == "🔄 Check now")
async def cmd_check_now(message: types.Message):
    global last_manual_check
    if not only_me(message): return
    import time
    now_ts = time.time()
    if now_ts - last_manual_check < CHECK_NOW_COOLDOWN:
        wait = int((CHECK_NOW_COOLDOWN - (now_ts - last_manual_check)) / 60) + 1
        await message.answer(f"⏳ hold up ~{wait} min before checking again.")
        return
    last_manual_check = now_ts
    rows = await get_urls()
    if not rows:
        await message.answer("📭 nothing here yet.", reply_markup=main_keyboard())
        return
    status_msg = await message.answer(f"🔍 checking {len(rows)} site(s)...")
    import time

    changes = []
    downs   = []
    ups     = []

    async with aiohttp.ClientSession() as session:
        for url_id, url, title, last_hash, last_text, is_up in rows:
            try:
                label = title if title else extract_domain(url)
                _, new_hash, new_text = await fetch_page(session, url)

                if new_hash is None:
                    fail_counts[url_id] = fail_counts.get(url_id, 0) + 1
                    if fail_counts[url_id] == 3 and is_up:
                        await set_status(url_id, False)
                        downs.append(f"🔴 <b>{label}</b>  <i>{extract_domain(url)}</i>")
                    continue

                if not is_up:
                    await set_status(url_id, True)
                    ups.append(f"🟢 <b>{label}</b>  <i>{extract_domain(url)}</i>")

                fail_counts[url_id] = 0

                if last_hash is None:
                    await update_page(url_id, new_hash, "\n".join(new_text))
                elif new_hash != last_hash:
                    now_ts = time.time()
                    old_text = last_text.split("\n") if last_text else []
                    diff = build_diff(old_text, new_text)
                    await update_page(url_id, new_hash, "\n".join(new_text))
                    last_notified[url_id] = now_ts
                    entry = f"<b>{label}</b>  <i>{extract_domain(url)}</i>"
                    if diff:
                        entry += f"\n{diff}"
                    changes.append((entry, url))

            except Exception as e:
                logging.error(f"check_now error for {url}: {e}")

    # build result message
    parts = []
    if changes:
        lines = "\n\n".join(f"• {e}" for e, _ in changes)
        parts.append(f"✨ {len(changes)} change(s)\n\n{lines}")
    if downs:
        parts.append("site(s) down:\n" + "\n".join(downs))
    if ups:
        parts.append("site(s) back up:\n" + "\n".join(ups))

    if parts:
        await status_msg.edit_text(
            "\n\n".join(parts) + f"\n\n· {now_str()} ·",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await status_msg.edit_text(
            f"✅ done. {len(rows)} sites, nothing new.\n\n· {now_str()} ·",
            parse_mode="HTML",
        )

@dp.message(F.text)
async def unknown_text(message: types.Message):
    if not only_me(message): return
    await message.answer(
        "🔗 send me a url and i’ll monitor it.\n\n<i>example: https://example.com</i>",
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "manage")
async def enter_manage(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    rows = await get_urls()
    if not rows:
        await callback.message.edit_text("📭 nothing to remove.")
        await callback.answer()
        return
    lines = "\n".join(
        f"{i}. {'🟢' if r[5] else '🔴'} {r[2] or extract_domain(r[1])}"
        for i, r in enumerate(rows, 1)
    )
    await callback.message.edit_text(
        f"📋 <b>which site should i stop tracking?</b>\n\n{lines}",
        parse_mode="HTML",
        reply_markup=manage_keyboard(rows),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pick:"))
async def pick_site(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    idx = int(callback.data.split(":")[1])
    rows = await get_urls()
    if idx >= len(rows):
        await callback.answer("site not found")
        return
    _, url, title, *__ = rows[idx]
    label = title or extract_domain(url)
    url_key = hashlib.md5(url.encode()).hexdigest()[:12]
    await callback.message.edit_text(
        f"🗑 stop tracking <b>{label}</b>?",
        parse_mode="HTML",
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

    if not rows:
        await callback.message.edit_text("🗑 all sites removed.")
        await callback.answer("done")
        return

    # go back to updated list
    up = sum(1 for r in rows if r[5])
    down = len(rows) - up
    summary = f"🟢 {up} up" + (f"  🔴 {down} down" if down else "")
    lines = "\n".join(
        f"{'🟢' if r[5] else '🔴'} {r[2] or extract_domain(r[1])}"
        for r in rows
    )
    await callback.message.edit_text(
        f"🗑 removed.\n\n📋 <b>now tracking {len(rows)} site(s)</b>  {summary}\n\n{lines}",
        parse_mode="HTML",
        reply_markup=list_view_keyboard(),
    )
    await callback.answer("done")

@dp.callback_query(F.data == "cancel_manage")
async def cancel_manage(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    rows = await get_urls()
    up = sum(1 for r in rows if r[5])
    down = len(rows) - up
    summary = f"🟢 {up} up" + (f"  🔴 {down} down" if down else "")
    lines = "\n\n".join(
        f"{'🟢' if r[5] else '🔴'} <b>{i}. <a href=\"{r[1]}\">{(r[2] or extract_domain(r[1]))[:35]}</a></b>"
        + (f"\n    <code>{extract_domain(r[1])}</code>" if r[2] else "")
        for i, r in enumerate(rows, 1)
    )
    await callback.message.edit_text(
        f"📋 <b>{len(rows)} site(s) in my list</b>  {summary}\n\n{lines}",
        parse_mode="HTML",
        reply_markup=list_view_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer()

@dp.callback_query(F.data == "ask_delete_all")
async def ask_delete_all(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    await callback.message.edit_text(
        "🗑 <b>remove all sites?</b>\n\nthis can’t be undone.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, delete all", callback_data="confirm_delete_all"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_manage"),
        ]])
    )
    await callback.answer()

@dp.callback_query(F.data == "confirm_delete_all")
async def confirm_delete_all(callback: types.CallbackQuery):
    if callback.from_user.id != ALLOWED_ID: return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM urls")
        await db.commit()
    fail_counts.clear()
    last_notified.clear()
    await callback.message.edit_text("🗑 all sites removed.")
    await callback.answer("done")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(check_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
