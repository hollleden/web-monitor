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
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ALLOWED_ID  = int(os.environ["ALLOWED_ID"])
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "3600"))
DB_PATH     = os.getenv("DB_PATH", "monitor.db")
TZ_OFFSET   = int(os.getenv("TZ_OFFSET", "0"))
WELCOME_IMG = "https://images4.imagebam.com/eb/f6/e5/ME1BIOEJ_o.png"
PAGE_SIZE   = 5 

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

def build_diff(old_text: list[str], new_text: list[str]) -> str:
    old_set = set(old_text)
    new_set = set(new_text)
    added = [l for l in new_text if l not in old_set and len(l) > 40]
    removed = [l for l in old_text if l not in new_set and len(l) > 40]
    if not added and not removed: return ""
    diff_content = "\n".join([f"+ {l[:80]}..." for l in added[:3]] + [f"- {l[:80]}..." for l in removed[:3]])
    return f"<tg-spoiler>{diff_content}</tg-spoiler>"

# -- Keyboards --
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="[ list ]"), KeyboardButton(text="[ scan ]")],
            [KeyboardButton(text="[ help ]")],
        ], resize_keyboard=True
    )

def list_view_keyboard(page: int, total_pages: int):
    buttons = []
    nav_row = [
        InlineKeyboardButton(text="[ < ]" if page > 0 else " ", callback_data=f"page:{page-1}" if page > 0 else "noop"),
        InlineKeyboardButton(text=f"{page+1} / {total_pages}", callback_data="noop"),
        InlineKeyboardButton(text="[ > ]" if page < total_pages - 1 else " ", callback_data=f"page:{page+1}" if page < total_pages - 1 else "noop")
    ]
    buttons.append(nav_row)
    buttons.append([
        InlineKeyboardButton(text="[ edit ]", callback_data="manage"),
        InlineKeyboardButton(text="[ clr all ]", callback_data="ask_delete_all"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def manage_keyboard(rows):
    buttons = []
    row = []
    for i, (url_id, url, title, _, __, is_up, *rest) in enumerate(rows, 1):
        status = "(+)" if is_up else "(!!)"
        label = (title or extract_domain(url))[:12]
        row.append(InlineKeyboardButton(text=f"{status} {i}. {label}", callback_data=f"pick:{i-1}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="<- back", callback_data="cancel_manage")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# -- DB & Logic --
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                last_hash TEXT,
                last_text TEXT,
                is_up INTEGER DEFAULT 1,
                last_changed TEXT,
                last_diff TEXT
            )
        """)
        await db.commit()

async def get_urls():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, url, title, last_hash, last_text, is_up, last_changed, last_diff FROM urls") as cur:
            rows = await cur.fetchall()
            return sorted(rows, key=lambda x: x[5])

async def run_checks(session: aiohttp.ClientSession):
    rows = await get_urls()
    changes, downs, ups = [], [], []
    for url_id, url, title, last_hash, last_text, is_up, last_changed, last_diff in rows:
        try:
            label = title or extract_domain(url)
            async with session.get(url, headers={"User-Agent": "mzekali/1.0"}, timeout=15) as resp:
                html = await resp.text()
                new_hash = hashlib.md5(html.encode()).hexdigest()
                new_text = html_to_text(html)
                if not is_up:
                    await set_status(url_id, True)
                    ups.append(f"site: <a href='{url}'>{label}</a>")
                if last_hash and new_hash != last_hash:
                    diff = build_diff(last_text.split("\n") if last_text else [], new_text)
                    if diff:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE urls SET last_hash=?, last_text=?, is_up=1, last_changed=?, last_diff=? WHERE id=?", (new_hash, "\n".join(new_text), now_str(), diff, url_id))
                            await db.commit()
                        changes.append((url, label, diff))
                elif not last_hash:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE urls SET last_hash=?, last_text=?, is_up=1 WHERE id=?", (new_hash, "\n".join(new_text), url_id))
                        await db.commit()
        except:
            if is_up:
                await set_status(url_id, False)
                downs.append(f"site: <a href='{url}'>{label or url}</a>\n└ status: <i>lost</i>")
    return changes, downs, ups

async def set_status(url_id: int, is_up: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE urls SET is_up=? WHERE id=?", (1 if is_up else 0, url_id))
        await db.commit()

# -- Handlers --
def format_site_entry(r) -> str:
    _, url, title, _, __, is_up, last_changed, last_diff = r
    status = "[ok]" if is_up else "[!!]"
    label = (title or extract_domain(url))[:35]
    res = f"{status} <a href='{url}'>{label}</a>"
    if not is_up: res += "\n└ status: <i>error</i>"
    elif last_changed:
        res += f"\n└ updated: {last_changed}"
        if last_diff: res += f"\n└ diff: {last_diff}"
    else: res += "\n└ status: <i>idle</i>"
    return res

async def show_list(message: types.Message, page: int = 0, edit: bool = False):
    rows = await get_urls()
    if not rows:
        text = "list is empty."
        if edit: await message.edit_text(text)
        else: await message.answer(text, reply_markup=main_keyboard())
        return
    total_pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start, end = page * PAGE_SIZE, (page + 1) * PAGE_SIZE
    up = sum(1 for r in rows if r[5])
    header = f"~~ <b>mzekali's watch-list</b> ({len(rows)})\n(+) {up} up | (!!) {len(rows)-up} down\n\n"
    body = "\n\n".join(format_site_entry(r) for r in rows[start:end])
    kb = list_view_keyboard(page, total_pages)
    if edit: await message.edit_text(header + body, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else: await message.answer(header + body, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

@dp.message(F.text.in_({"[ list ]", "📋 watch-list", "📋 My list"}))
async def cmd_list(message: types.Message):
    if message.from_user.id == ALLOWED_ID: await show_list(message)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ALLOWED_ID:
        await message.answer_photo(photo=WELCOME_IMG, caption="~ <b>mzekali is watching.</b>", parse_mode="HTML", reply_markup=main_keyboard())

@dp.message(F.text.in_({"[ scan ]", "🔍 scan now"}))
async def cmd_scan(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    status = await message.answer("scanning...")
    async with aiohttp.ClientSession() as session:
        changes, downs, ups = await run_checks(session)
    for url, label, diff in changes:
        await bot.send_message(ALLOWED_ID, f">> <b>site updated</b>\n\nsite: <a href='{url}'>{label}</a>\nat: {now_str()}\n\ndiff:\n{diff}", parse_mode="HTML", disable_web_page_preview=True)
    if downs: await bot.send_message(ALLOWED_ID, "!!! <b>lost:</b>\n\n" + "\n".join(downs), parse_mode="HTML")
    if ups: await bot.send_message(ALLOWED_ID, "+++ <b>back:</b>\n\n" + "\n".join(ups), parse_mode="HTML")
    await status.edit_text(f"done.\nat: {now_str()}")

@dp.callback_query(F.data.startswith("page:"))
async def cb_page(callback: types.CallbackQuery):
    await show_list(callback.message, page=int(callback.data.split(":")[1]), edit=True)
    await callback.answer()

@dp.callback_query(F.data == "manage")
async def cb_manage(callback: types.CallbackQuery):
    rows = await get_urls()
    await callback.message.edit_text("edit: <b>select to remove:</b>", parse_mode="HTML", reply_markup=manage_keyboard(rows))

@dp.callback_query(F.data == "cancel_manage")
async def cb_cancel(callback: types.CallbackQuery):
    await show_list(callback.message, edit=True)

@dp.message(F.text.startswith("http"))
async def auto_add(message: types.Message):
    if message.from_user.id != ALLOWED_ID: return
    url = normalize_url(message.text)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                html = await resp.text()
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("INSERT OR IGNORE INTO urls (url, title, last_hash, last_text) VALUES (?, ?, ?, ?)", (url, extract_domain(url), hashlib.md5(html.encode()).hexdigest(), "\n".join(html_to_text(html))))
                    await db.commit()
                await message.answer(f"added: <code>{url}</code>", parse_mode="HTML")
    except: await message.answer("failed to reach site.")

@dp.callback_query(F.data == "noop")
async def cb_noop(c: types.CallbackQuery): await c.answer()

async def main():
    await init_db()
    asyncio.create_task(check_loop())
    await dp.start_polling(bot)

async def check_loop():
    while True:
        await asyncio.sleep(CHECK_EVERY)
        try:
            async with aiohttp.ClientSession() as session:
                changes, downs, ups = await run_checks(session)
                # Уведомления аналогично cmd_scan
        except: pass

if __name__ == "__main__":
    asyncio.run(main())
