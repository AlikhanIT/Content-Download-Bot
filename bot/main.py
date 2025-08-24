import os
import asyncio
import subprocess
import tempfile
import json

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

API_TOKEN = os.getenv("API_TOKEN")  # токен
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://localhost:8081")

session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL), timeout=1800)
bot = Bot(token=API_TOKEN, session=session)
dp = Dispatcher()


# --- Вспомогательные функции --- #

async def run_cmd(cmd: list[str]):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Ошибка: {err.decode()}")
    return out.decode()


async def get_formats(url: str):
    cmd = ["yt-dlp", "-j", url]
    out = await run_cmd(cmd)
    info = json.loads(out)
    formats = [
        {
            "format_id": f["format_id"],
            "ext": f["ext"],
            "resolution": f.get("resolution") or f"{f.get('height','?')}p",
            "filesize": f.get("filesize") or 0,
            "url": f["url"],
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec")
        }
        for f in info["formats"] if f.get("url")
    ]
    return formats, info.get("title", "video")


async def download_with_tordl(url: str, out_file: str, port="9050"):
    cmd = [
        "./tor-dl",
        "-c", "16",
        "-ports", port,
        "-rps", "8",
        "-segment-size", "1048576",
        "-tail-threshold", "33554432",
        "-tail-workers", "1",
        "-user-agent", "Mozilla/5.0",
        "-referer", "https://www.youtube.com/",
        "-force",
        "-n", out_file,
        url
    ]
    await run_cmd(cmd)
    return out_file


async def merge_audio_video(video_file, audio_file, output_file):
    cmd = [
        "ffmpeg", "-y",
        "-i", video_file,
        "-i", audio_file,
        "-c", "copy",
        output_file
    ]
    await run_cmd(cmd)
    return output_file


# --- Хэндлеры --- #

@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    await msg.answer("Отправь мне ссылку на YouTube 🎬")


@dp.message(F.text.regexp(r"https?://(www\.)?(youtube\.com|youtu\.be)/"))
async def handle_youtube(msg: types.Message):
    url = msg.text.strip()
    await msg.answer("Получаю форматы...")
    formats, title = await get_formats(url)

    kb = InlineKeyboardBuilder()
    for f in formats:
        if f["vcodec"] != "none" and f["acodec"] == "none":  # только видео
            kb.button(
                text=f"{f['resolution']} ({f['ext']})",
                callback_data=f"dl|{url}|{f['format_id']}|{title}"
            )
    kb.adjust(1)
    await msg.answer(f"Выбери качество для: {title}", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("dl|"))
async def handle_download(cb: types.CallbackQuery):
    _, url, fmt, title = cb.data.split("|", 3)

    await cb.message.answer("Скачиваю через Tor...")

    with tempfile.TemporaryDirectory() as tmpdir:
        video_out = os.path.join(tmpdir, "video.mp4")
        audio_out = os.path.join(tmpdir, "audio.m4a")
        final_out = os.path.join(tmpdir, f"{title}.mp4")

        # 1) Видео
        video_url = await run_cmd(["yt-dlp", "-f", fmt, "-g", url])
        await download_with_tordl(video_url.strip(), video_out)

        # 2) Аудио (лучшее доступное)
        audio_url = await run_cmd(["yt-dlp", "-f", "bestaudio", "-g", url])
        await download_with_tordl(audio_url.strip(), audio_out)

        # 3) Мерж
        await merge_audio_video(video_out, audio_out, final_out)

        # 4) Отправка
        await cb.message.answer_document(types.FSInputFile(final_out))

    await cb.message.answer("✅ Готово!")


# --- MAIN --- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
