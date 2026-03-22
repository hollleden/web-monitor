import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from pipeline import process_media
from database import init_db
from digest import send_weekly_digest, send_quarterly_digest

load_dotenv()

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_ID = int(os.getenv("ALLOWED_ID"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    if message.from_user.id != ALLOWED_ID:
        return
    await message.answer(
        "👋 Hola! Я Listo — твой второй мозг.\n\n"
        "Скидывай мне фото или видео из TikTok/Reels — "
        "я прочитаю, разберу и сохраню.\n\n"
        "Каждое воскресенье пришлю дайджест всего сохранённого 🗞"
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    if message.from_user.id != ALLOWED_ID:
        return
    await message.answer("⚙️ Читаю...")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    result = await process_media(file_bytes.read(), media_type="image")
    await message.answer(result, parse_mode="Markdown")


@dp.message(F.video | F.document)
async def handle_video(message: Message):
    if message.from_user.id != ALLOWED_ID:
        return
    await message.answer("⚙️ Обрабатываю видео, займёт ~20 секунд...")

    video = message.video or message.document
    file = await bot.get_file(video.file_id)
    file_bytes = await bot.download_file(file.file_path)
    result = await process_media(file_bytes.read(), media_type="video")
    await message.answer(result, parse_mode="Markdown")


async def main():
    init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_weekly_digest,
        "cron",
        day_of_week="sun",
        hour=10,
        minute=0,
        args=[bot, ALLOWED_ID],
    )
    scheduler.add_job(
        send_quarterly_digest,
        "cron",
        month="1,4,7,10",
        day=1,
        hour=10,
        minute=0,
        args=[bot, ALLOWED_ID],
    )
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
