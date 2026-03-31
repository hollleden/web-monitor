import asyncio
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

# -- Config --
BOT_TOKEN          = os.environ["BOT_TOKEN"]
ALLOWED_ID         = int(os.environ["ALLOWED_ID"])
CHECK_EVERY        = int(os.getenv("CHECK_EVERY", "3600"))
DB_PATH            = os.getenv("DB_PATH", "monitor.db")
TZ_OFFSET          = int(os.getenv("TZ_OFFSET", "0"))
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
WELCOME_IMG        = "https://images4.imagebam.com/eb/f6/e5/ME1BIOEJ_o.png"
PAGE_SIZE          = 5

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# -- Utils --
def now_str() -> str:
    tz = timezone(timedelta(hours=TZ_OFFSET))
    return datetime.now(tz).strftime("%d %b, %H:%M")

def extract_domain(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").split("/")[0]

def normalize_url(url: str) -> str:
    url = url.split()[0].strip()
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url.rstrip("/")

def html_to_text(html: str) -> list[str]:
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return [l.strip() for l in text.split(".") if len(l.strip()) > 20]

def build_diff(old_text: list[str], new_text: list[str]) -> tuple[str, list[str], list[str]]:
    """Returns (spoiler_text, added_lines, removed_lines)."""
    old_set = set(old_text)
    new_set = set(new_text)
    added   = [l for l in new_text if l not in old_set and len(l) > 40]
    removed = [l for l in old_text if l not in new_set and len(l) > 40]
    if not added and not removed:
        return "", [], []
    parts = [f"+ {l[:80]}" for l in added[:3]] + [f"- {l[:80]}" for l in removed[:3]]
    return f"<tg-spoiler>{chr(10).join(parts)}</tg-spoiler>", added, removed

async def ai_summary(session: aiohttp.ClientSession, url: str, label: str,
                     added: list[str], removed: list[str]) -> str:
    """Ask Claude to summarize the diff in plain English. Falls back to raw diff."""
    if not ANTHROPIC_API_KEY or (not added and not removed):
        return ""
    added_text   = "\n".join(added[:10])
    removed_text = "\n".join(removed[:10])
    prompt = (
        f"A webpage changed. Summarize what happened in 1-2 short sentences, plain English.\n"
        f"Site: {label} ({extract_domain(url)})\n"
        f"Added text:\n{added_text}\n"
        f"Removed text:\n{removed_text}\n\n"
        f"Reply with ONLY the summary, no preamble."
    )
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
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            return data["content"][0]["text"].strip()
    except Exception as e:
        logging.warning(f"ai_summary error: {e}")
        return ""

# -- Keyboards --
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="[ list ]"), KeyboardButton(text="[ scan ]")],
            [KeyboardButton(text="[ help ]")],
        ], resize_keyboard=True
    )

def list_view_keyboard(page: int, total_pages: int):
    nav_row = [
        InlineKeyboardButton(text="[ < ]" if page > 0 else " ",
                             callback_data=f"page:{page-1}" if page > 0 else "noop"),
        InlineKeyboardButton(text=f"{page+1} / {total_pages}", callback_data="noop"),
        InlineKeyboardButton(text="[ > ]" if page < total_pages - 1 else " ",
                             callback_data=f"page:{page+1}" if page < total_pages - 1 else "noop"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        nav_row,
        [
            InlineKeyboardButton(text="[ edit ]", callback_data="manage"),
            InlineKeyboardButton(text="[ clr all ]", callback_data="ask_delete_all"),
        ],
    ])

def manage_keyboard(rows):
    buttons = []
    row = []
    for i, (url_id, url, title, _, __, is_up, *rest) in enumerate(rows, 1):
        status = "(+)" if is_up else "(!!)"
        label  = (title or extract_domain(url))[:12]
        row.append(InlineKeyboardButton(text=f"{status} {i}. {label}", callback_data=f"pick:{i-1}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="<- back", callback_data="cancel_manage")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_delete_keyboard(url_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="yes, remove", callback_data=f"del:{url_key}"),
        InlineKeyboardButton(text="no", callback_data="cancel_manage"),
    ]])

# -- DB --
async def init_db():
    if os.path.dirname(DB_PATH):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                url          TEXT UNIQUE NOT NULL,
                title        TEXT,
                last_hash    TEXT,
                last_text    TEXT,
                is_up        INTEGER DEFAULT 1,
                last_changed TEXT,
                last_diff    TEXT,
                last_summary TEXT
            )
        """)
        for col, definition in [
            ("last_changed", "TEXT"),
            ("last_diff",    "TEXT"),
            ("last_summary", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE urls ADD COLUMN {col} {definition}")
            except Exception:
                pass
        await db.commit()

async def get_urls():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, url, title, last_hash, last_text, is_up, last_changed, last_diff, last_summary FROM urls"
        ) as cur:
            rows = await cur.fetchall()
    return sorted(rows, key=lambda x: x[5])

async def url_exists(url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM urls WHERE url=?", (url,)) as cur:
            return await cur.fetchone() is not None

async def set_status(url_id: int, is_up: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE urls SET is_up=? WHERE id=?", (1 if is_up else 0, url_id))
        await db.commit()

async def remove_url_by_key(url_key: str):
    rows = await get_urls()
    for row in rows:
        url_id, url = row[0], row[1]
        if hashlib.md5(url.encode()).hexdigest()[:12] == url_key:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM urls WHERE id=?", (url_id,))
                await db.commit()
            return url

# -- Core check --
async def run_checks(session: aiohttp.ClientSession):
    rows = await get_urls()
    changes, downs, ups = [], [], []

    for row in rows:
        url_id, url, title, last_hash, last_text, is_up, last_changed, last_diff, last_summary = row
        label = title or extract_domain(url)
        try:
            async with session.get(
                url,
                headers={"User-Agent": "mzekali/1.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html     = await resp.text()
                new_hash = hashlib.md5(html.encode()).hexdigest()
                new_text = html_to_text(html)

            if not is_up:
                await set_status(url_id, True)
                ups.append((url, label))

            if last_hash and new_hash != last_hash:
                spoiler, added, removed = build_diff(
                    last_text.split("\n") if last_text else [], new_text
                )
                summary = await ai_summary(session, url, label, added, removed)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE urls SET last_hash=?, last_text=?, is_up=1, "
                        "last_changed=?, last_diff=?, last_summary=? WHERE id=?",
                        (new_hash, "\n".join(new_text), now_str(),
                         spoiler, summary, url_id)
                    )
                    await db.commit()
                if spoiler or summary:
                    changes.append((url, label, spoiler, summary))

            elif not last_hash:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE urls SET last_hash=?, last_text=?, is_up=1 WHERE id=?",
                        (new_hash, "\n".join(new_text), url_id)
                    )
                    await db.commit()

        except Exception as e:
            logging.warning(f"check error {url}: {e}")
            if is_up:
                await set_status(url_id, False)
                downs.append((url, label))

    return changes, downs, ups

# -- Format --
def format_site_entry(row) -> str:
    _, url, title, _, __, is_up, last_changed, last_diff, last_summary = row
    label  = (title or extract_domain(url))[:35]
    status = "[ok]" if is_up else "[!!]"
    res    = f"{status} <a href='{url}'>{label}</a>"
    if not is_up:
        res += "\n└ <i>not responding</i>"
    elif last_changed:
        res += f"\n└ {last_changed}"
        if last_summary:
            res += f"\n└ {last_summary}"
        if last_diff:
            res += f"\n└ {last_diff}"
    else:
        res += "\n└ <i>idle</i>"
    return res

def format_change_notification(url: str, label: str, spoiler: str, summary: str) -> str:
    res = f"<a href='{url}'><b>{label}</b></a>\n└ {now_str()}"
    if summary:
        res += f"\n└ {summary}"
    if spoiler:
        res += f"\n└ {spoiler}"
    return res

async def show_list(target, page: int = 0, edit: bool = False):
    rows = await get_urls()
    if not rows:
        text = "list is empty."
        if edit:
            await target.edit_text(text)
        else:
            await target.answer(text, reply_markup=main_keyboard())
        return
    total_pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page  = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end   = start + PAGE_SIZE
    up    = sum(1 for r in rows if r[5])
    header = (
        f"~~ <b>mzekali's watch-list</b> ({len(rows)})\n"
        f"(+) {up} up  |  (!!) {len(rows)-up} down\n\n"
    )
    body = "\n\n".join(format_site_entry(r) for r in rows[start:end])
    kb   = list_view_keyboard(page, total_pages)
    kwargs = dict(parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    if edit:
        await target.edit_text(header + body, **kwargs)
    else:
        await target.answer(header + body, **kwargs)

# -- Handlers --
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    await message.answer_photo(
        photo=WELCOME_IMG,
        caption="~ <b>mzekali is watching.</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "[ help ]")
async def cmd_help(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    await message.answer(
        "💡 <b>quick guide:</b>\n\n"
        "🔗 send any url → i'll watch it\n"
        "[ list ] → see what i'm tracking\n"
        "[ scan ] → check right now\n"
        "[ edit ] → remove sites\n\n"
        "[ok] up  /  [!!] down\n\n"
        "i check every hour and tell you what changed.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

@dp.message(F.text == "[ list ]")
async def cmd_list(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    await show_list(message)

@dp.message(F.text == "[ scan ]")
async def cmd_scan(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    status_msg = await message.answer("scanning...")
    async with aiohttp.ClientSession() as session:
        changes, downs, ups = await run_checks(session)

    parts = []
    if changes:
        entries = "\n\n".join(f"• {format_change_notification(u, l, s, sm)}" for u, l, s, sm in changes)
        parts.append(f"✨ {len(changes)} update(s)\n\n{entries}")
    if downs:
        parts.append("🚫 not responding:\n" + "\n".join(f"• <a href='{u}'>{l}</a>" for u, l in downs))
    if ups:
        parts.append("✅ back online:\n" + "\n".join(f"• <a href='{u}'>{l}</a>" for u, l in ups))

    if parts:
        await status_msg.edit_text(
            "\n\n".join(parts) + f"\n\n· {now_str()} ·",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await status_msg.edit_text(f"✅ done. nothing new.\n· {now_str()} ·", parse_mode="HTML")

@dp.message(F.text.startswith("http"))
async def auto_add(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    url = normalize_url(message.text)
    if await url_exists(url):
        await message.answer(f"👀 already tracking that.")
        return
    status_msg = await message.answer("🔍 checking site...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"User-Agent": "mzekali/1.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html = await resp.text()
            # get smart title
            title = extract_domain(url)
            start = html.lower().find("<title>")
            end   = html.lower().find("</title>")
            if start != -1 and end != -1:
                raw = html[start+7:end].strip()[:50]
                if raw:
                    title = raw
            if ANTHROPIC_API_KEY:
                short = await ai_summary.__wrapped__(session, url, title, [], []) if hasattr(ai_summary, '__wrapped__') else None
                # use smart_title prompt instead
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
                            "messages": [{"role": "user", "content":
                                f"Short 2-4 word label: [Company] [Page]. "
                                f"Domain: {extract_domain(url)}, Title: {title}. "
                                f"Reply ONLY the label."}]
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp2:
                        data = await resp2.json()
                        title = data["content"][0]["text"].strip().strip('"') or title
                except Exception:
                    pass
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO urls (url, title, last_hash, last_text) VALUES (?, ?, ?, ?)",
                    (url, title, hashlib.md5(html.encode()).hexdigest(), "\n".join(html_to_text(html)))
                )
                await db.commit()
            await status_msg.edit_text(
                f"✅ <b>got it.</b>\n\n📄 {title}\n<code>{url}</code>",
                parse_mode="HTML"
            )
    except Exception as e:
        await status_msg.edit_text(f"❌ couldn't reach it.\n\n<code>{url}</code>", parse_mode="HTML")

@dp.message(F.text)
async def unknown_text(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    await message.answer("🔗 send me a url to monitor it.")

# -- Callbacks --
@dp.callback_query(F.data.startswith("page:"))
async def cb_page(callback: types.CallbackQuery):
    await show_list(callback.message, page=int(callback.data.split(":")[1]), edit=True)
    await callback.answer()

@dp.callback_query(F.data == "manage")
async def cb_manage(callback: types.CallbackQuery):
    rows = await get_urls()
    await callback.message.edit_text(
        "edit: <b>select to remove:</b>",
        parse_mode="HTML",
        reply_markup=manage_keyboard(rows),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(callback: types.CallbackQuery):
    idx  = int(callback.data.split(":")[1])
    rows = await get_urls()
    if idx >= len(rows):
        await callback.answer("not found")
        return
    row     = rows[idx]
    url     = row[1]
    title   = row[2] or extract_domain(url)
    url_key = hashlib.md5(url.encode()).hexdigest()[:12]
    await callback.message.edit_text(
        f"remove <b>{title}</b>?",
        parse_mode="HTML",
        reply_markup=confirm_delete_keyboard(url_key),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("del:"))
async def cb_del(callback: types.CallbackQuery):
    url_key = callback.data.split(":")[1]
    await remove_url_by_key(url_key)
    await show_list(callback.message, edit=True)
    await callback.answer("removed")

@dp.callback_query(F.data == "cancel_manage")
async def cb_cancel(callback: types.CallbackQuery):
    await show_list(callback.message, edit=True)
    await callback.answer()

@dp.callback_query(F.data == "ask_delete_all")
async def cb_ask_delete_all(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "remove <b>all</b> sites?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="yes, all", callback_data="confirm_delete_all"),
            InlineKeyboardButton(text="no", callback_data="cancel_manage"),
        ]])
    )
    await callback.answer()

@dp.callback_query(F.data == "confirm_delete_all")
async def cb_confirm_delete_all(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM urls")
        await db.commit()
    await callback.message.edit_text("all removed.")
    await callback.answer("done")

@dp.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()

# -- Background loop --
async def check_loop():
    await asyncio.sleep(10)
    while True:
        try:
            tz      = timezone(timedelta(hours=TZ_OFFSET))
            now_dt  = datetime.now(tz)
            next_hr = now_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            await asyncio.sleep((next_hr - now_dt).total_seconds())

            async with aiohttp.ClientSession() as session:
                changes, downs, ups = await run_checks(session)

            parts = []
            if changes:
                entries = "\n\n".join(f"• {format_change_notification(u, l, s, sm)}" for u, l, s, sm in changes)
                parts.append(f"✨ {len(changes)} update(s)\n\n{entries}")
            if downs:
                parts.append("🚫 not responding:\n" + "\n".join(f"• <a href='{u}'>{l}</a>" for u, l in downs))
            if ups:
                parts.append("✅ back online:\n" + "\n".join(f"• <a href='{u}'>{l}</a>" for u, l in ups))
            if parts:
                await bot.send_message(
                    ALLOWED_ID,
                    "\n\n".join(parts) + f"\n\n· {now_str()} ·",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        except Exception as e:
            logging.error(f"check_loop: {e}")

# -- Main --
async def main():
    await init_db()
    asyncio.create_task(check_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
