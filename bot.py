import asyncio
import hashlib
import logging
import os

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]       # Telegram bot token
ALLOWED_ID  = int(os.environ["ALLOWED_ID"]) # Your Telegram chat_id (only you can use the bot)
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "300"))  # seconds between checks (default 5 min)
DB_PATH     = "monitor.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

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
    await asyncio.sleep(10)  # small delay on startup
    async with aiohttp.ClientSession() as session:
        while True:
            rows = await get_urls()
            for url_id, url, last_hash in rows:
                new_hash = await fetch_hash(session, url)
                if new_hash is None:
                    continue  # skip on error, try next cycle

                if last_hash is None:
                    # first check — just save hash, don't notify
                    await update_hash(url_id, new_hash)
                elif new_hash != last_hash:
                    await update_hash(url_id, new_hash)
                    await bot.send_message(
                        ALLOWED_ID,
                        f"🔔 <b>Изменения обнаружены!</b>\n\n🔗 {url}",
                        parse_mode="HTML"
                    )
            await asyncio.sleep(CHECK_EVERY)

# ── Handlers ──────────────────────────────────────────────────────────────────
def only_me(message: types.Message) -> bool:
    return message.from_user.id == ALLOWED_ID

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not only_me(message): return
    await message.answer(
        "👋 <b>Web Monitor Bot</b>\n\n"
        "/add &lt;url&gt; — добавить сайт\n"
        "/list — список отслеживаемых\n"
        "/remove &lt;id&gt; — удалить сайт\n"
        f"\n⏱ Проверка каждые {CHECK_EVERY // 60} мин.",
        parse_mode="HTML"
    )

@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    if not only_me(message): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("http"):
        await message.answer("❌ Укажи URL: /add https://example.com")
        return
    url = parts[1].strip()
    await add_url(url)
    await message.answer(f"✅ Добавлено:\n{url}")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    if not only_me(message): return
    rows = await get_urls()
    if not rows:
        await message.answer("📭 Список пуст. Добавь сайт: /add https://...")
        return
    text = "\n".join(f"<code>{r[0]}</code>. {r[1]}" for r in rows)
    await message.answer(f"📋 <b>Отслеживаю:</b>\n\n{text}", parse_mode="HTML")

@dp.message(Command("remove"))
async def cmd_remove(message: types.Message):
    if not only_me(message): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("❌ Укажи ID: /remove 3\nID смотри в /list")
        return
    await remove_url(int(parts[1].strip()))
    await message.answer("🗑 Удалено.")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(check_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
