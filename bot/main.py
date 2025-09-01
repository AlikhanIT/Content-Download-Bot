import os
import asyncio
import tempfile
import uuid
import platform
import logging
import sys
import json
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================== CONFIG ================== #
API_TOKEN = os.getenv("API_TOKEN")
BASE_PORT = 9050

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ================== ЛОГГЕР ================== #
logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
handler.setFormatter(formatter)
logger.addHandler(handler)

def log(msg: str, level="info"):
    getattr(logger, level)(msg)

# ================== УТИЛИТЫ ================== #
async def run_cmd(cmd: list[str], cwd=None, capture=False):
    log(f"▶ Запуск: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log(f"[STDERR] {stderr.decode()}", "error")
        raise RuntimeError(f"Команда упала: {' '.join(cmd)}")
    return stdout.decode() if capture else ""

def get_tor_dl_path():
    if platform.system().lower().startswith("win"):
        return os.path.join(os.getcwd(), "tor-dl.exe")
    return os.path.join(os.getcwd(), "tor-dl")

async def download_with_tordl(url: str, out_file: str):
    fname = os.path.basename(out_file)
    workdir = os.path.dirname(out_file)
    cmd = [
        get_tor_dl_path(),
        "-c", "16",
        "-ports", str(BASE_PORT),
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
        raise RuntimeError("tor-dl не создал файл")
    return out_file

async def merge_av(video_file, audio_file, output_file):
    cmd = ["ffmpeg", "-y", "-i", video_file, "-i", audio_file, "-c", "copy", output_file]
    await run_cmd(cmd)
    return output_file

async def get_formats_ytdlp(url: str):
    cmd = [
        "yt-dlp",
        "--proxy", f"socks5://127.0.0.1:{BASE_PORT}",
        "-J",  # JSON output
        url
    ]
    out = await run_cmd(cmd, capture=True)
    info = json.loads(out)
    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") != "none" and f.get("acodec") == "none":
            size = f.get("filesize") or 0
            formats.append({
                "format_id": f["format_id"],
                "ext": f["ext"],
                "resolution": f.get("resolution") or f"{f.get('height','?')}p",
                "filesize": size
            })
    return formats, info.get("title", "video")

async def get_direct_url(url: str, fmt: str):
    cmd = [
        "yt-dlp",
        "--proxy", f"socks5://127.0.0.1:{BASE_PORT}",
        "-f", fmt,
        "--get-url",
        url
    ]
    out = await run_cmd(cmd, capture=True)
    return out.strip()

def sanitize_filename(name: str) -> str:
    # Разрешаем только буквы, цифры, пробелы, _, -, .
    safe = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ _\.-]", "", name)
    return safe.strip() or "video"

# ================== HANDLERS ================== #
DOWNLOAD_JOBS = {}

@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    await msg.answer("Отправь YouTube ссылку или прямую ссылку 🎬")

@dp.message(F.text.regexp(r"https?://\S+"))
async def handle_link(msg: types.Message):
    url = msg.text.strip()
    if "youtube.com" in url or "youtu.be" in url:
        await msg.answer("Получаю форматы через Tor...")
        try:
            formats, title = await get_formats_ytdlp(url)
        except Exception as e:
            await msg.answer(f"Ошибка yt-dlp: {e}")
            return

        if not formats:
            await msg.answer("Не нашёл подходящие форматы (только видео без аудио)")
            return

        kb = InlineKeyboardBuilder()
        for f in formats:
            size_mb = f["filesize"] / 1024 / 1024 if f["filesize"] else 0
            text = f"{f['resolution']} {f['ext']}"
            if size_mb:
                text += f" ~{int(size_mb)}MB"
            job_id = str(uuid.uuid4())[:8]
            DOWNLOAD_JOBS[job_id] = {"url": url, "fmt": f["format_id"], "title": title}
            kb.button(text=text, callback_data=f"dl|{job_id}")
        kb.adjust(1)
        await msg.answer(f"Выбери качество для: {title}", reply_markup=kb.as_markup())
    else:
        # Прямая ссылка
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, f"{uuid.uuid4().hex}.mp4")
            try:
                await download_with_tordl(url, out_file)
                await msg.answer_video(types.FSInputFile(out_file))
            except Exception as e:
                await msg.answer(f"Ошибка: {e}")

@dp.callback_query(F.data.startswith("dl|"))
async def handle_download(cb: types.CallbackQuery):
    _, job_id = cb.data.split("|", 1)
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        await cb.message.answer("❌ Задача не найдена")
        return
    url, fmt, title = job["url"], job["fmt"], job["title"]
    await cb.message.answer("⚡ Скачиваю через Tor...")

    with tempfile.TemporaryDirectory() as tmpdir:
        vfile, afile = os.path.join(tmpdir, "v.mp4"), os.path.join(tmpdir, "a.m4a")
        title_safe = sanitize_filename(title)
        final = os.path.join(tmpdir, f"{title_safe}.mp4")

        try:
            vurl = await get_direct_url(url, fmt)
            aurl = await get_direct_url(url, "bestaudio")
            await asyncio.gather(
                download_with_tordl(vurl, vfile),
                download_with_tordl(aurl, afile)
            )
            await merge_av(vfile, afile, final)
            await cb.message.answer_video(types.FSInputFile(final), caption=title)
        except Exception as e:
            await cb.message.answer(f"Ошибка: {e}")

# ================== MAIN ================== #
async def main():
    me = await bot.get_me()
    log(f"Бот @{me.username} запущен ✅")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
