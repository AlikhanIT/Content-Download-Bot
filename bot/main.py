import os
import asyncio
import subprocess
import tempfile
import json
import uuid
import aiohttp
from aiohttp_socks import ProxyConnector

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramRetryAfter


# ================== CONFIG ================== #
API_TOKEN = os.getenv("API_TOKEN")
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://telegram_bot_api:8081")
TOR_DL_BIN = os.getenv("TOR_DL_BIN", "tor-dl")

TOR_PROXY = "socks5://127.0.0.1:9050"
COOKIES = "cookies.txt" if os.path.exists("cookies.txt") else None

session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL), timeout=1800)
bot = Bot(token=API_TOKEN, session=session)
dp = Dispatcher()

DOWNLOAD_JOBS = {}  # job_id -> info dict


# ================== UTILS ================== #

def log(msg: str):
    print(f"[BOT] {msg}", flush=True)


async def run_cmd(cmd: list[str], cwd=None):
    log(f"▶ Запуск: {' '.join(cmd)} (cwd={cwd or os.getcwd()})")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    log(f"STDOUT:\n{out.decode()}")
    log(f"STDERR:\n{err.decode()}")
    if proc.returncode != 0:
        raise RuntimeError(f"Ошибка команды: {cmd}\n{err.decode()}")
    return out.decode()


async def get_formats(url: str):
    cmd = ["yt-dlp", "-j", "--proxy", TOR_PROXY]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    cmd.append(url)

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


async def resolve_redirects(url: str) -> str:
    connector = ProxyConnector.from_url(TOR_PROXY)
    async with aiohttp.ClientSession(connector=connector) as s:
        while True:
            async with s.head(url, allow_redirects=False) as resp:
                if 300 <= resp.status < 400 and "Location" in resp.headers:
                    url = resp.headers["Location"]
                else:
                    return url


async def get_direct_url(url: str, fmt: str):
    cmd = ["yt-dlp", "--proxy", TOR_PROXY, "-f", fmt, "-g"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    cmd.append(url)

    out = await run_cmd(cmd)
    raw_url = out.strip()
    final_url = await resolve_redirects(raw_url)
    return final_url

async def download_with_tordl(url: str, out_file: str, port="9050"):
    fname = os.path.basename(out_file)
    workdir = os.path.dirname(out_file)

    if "m3u8" in url or "hls_playlist" in url:
        cmd = [
            "ffmpeg", "-y",
            "-user_agent", "Mozilla/5.0",
            "-headers", "Referer: https://www.youtube.com/\r\n",
            "-i", url,
            "-c", "copy",
            fname
        ]
    else:
        cmd = [
            TOR_DL_BIN,   # 🔑 теперь бинарь ищется в PATH
            "-c", "16",
            "-ports", port,
            "-rps", "8",
            "-segment-size", "1048576",
            "-tail-threshold", "33554432",
            "-tail-workers", "1",
            "-user-agent", "Mozilla/5.0",
            "-referer", "https://www.youtube.com/",
            "-force",
            "-n", fname,
            url
        ]

    await run_cmd(cmd, cwd=workdir)
    if not os.path.exists(out_file):
        raise RuntimeError(f"tor-dl не создал файл: {out_file}")
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


# ================== HANDLERS ================== #

@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    await msg.answer("Отправь YouTube ссылку 🎬")


@dp.message(F.text.regexp(r"https?://(www\.)?(youtube\.com|youtu\.be)/"))
async def handle_youtube(msg: types.Message):
    url = msg.text.strip()
    await msg.answer("Получаю форматы через Tor...")

    try:
        formats, title = await get_formats(url)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")
        return

    kb = InlineKeyboardBuilder()
    for f in formats:
        if f["vcodec"] != "none" and f["acodec"] == "none":  # только видео
            size_mb = f["filesize"] / 1024 / 1024 if f["filesize"] else 0
            text = f"{f['resolution']} ({f['ext']})"
            if size_mb:
                text += f" ~{int(size_mb)}MB"

            job_id = str(uuid.uuid4())[:8]
            DOWNLOAD_JOBS[job_id] = {
                "url": url,
                "fmt": f["format_id"],
                "title": title
            }
            kb.button(text=text, callback_data=f"dl|{job_id}")

    kb.adjust(1)
    await msg.answer(f"Выбери качество для: {title}", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("dl|"))
async def handle_download(cb: types.CallbackQuery):
    _, job_id = cb.data.split("|", 1)

    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        await cb.message.answer("❌ Задача не найдена или устарела")
        return

    url, fmt, title = job["url"], job["fmt"], job["title"]
    await cb.message.answer("Скачиваю через Tor...")

    with tempfile.TemporaryDirectory() as tmpdir:
        video_out = os.path.join(tmpdir, "video.mp4")
        audio_out = os.path.join(tmpdir, "audio.m4a")
        final_out = os.path.join(tmpdir, f"{title}.mp4")

        try:
            video_url = await get_direct_url(url, fmt)

            audio_url = None
            for fallback in ["bestaudio", "140", "251"]:
                try:
                    audio_url = await get_direct_url(url, fallback)
                    break
                except Exception:
                    continue

            if not audio_url:
                raise RuntimeError("Не удалось найти аудиопоток")

            await asyncio.gather(
                download_with_tordl(video_url, video_out),
                download_with_tordl(audio_url, audio_out)
            )

            await merge_audio_video(video_out, audio_out, final_out)

            await cb.message.answer_document(types.FSInputFile(final_out))
            await cb.message.answer("✅ Готово!")
        except Exception as e:
            await cb.message.answer(f"❌ Ошибка загрузки: {e}")


# ================== MAIN ================== #

async def main():
    # фикс флуда на getMe
    while True:
        try:
            me = await bot.get_me()
            log(f"Бот @{me.username} запущен ✅")
            break
        except TelegramRetryAfter as e:
            log(f"Flood control: жду {e.retry_after} сек...")
            await asyncio.sleep(e.retry_after)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
