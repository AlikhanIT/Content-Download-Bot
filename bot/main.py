import os
import asyncio
import tempfile
import uuid
import platform
import logging
import sys
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp

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
async def run_cmd(cmd: list[str], cwd=None):
    log(f"▶ Запуск: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        log(f"[CMD] {line.decode().strip()}")
    err = await proc.stderr.read()
    await proc.wait()
    if proc.returncode != 0:
        log(f"[STDERR] {err.decode()}", "error")
        raise RuntimeError(f"Команда упала: {cmd}")
    return ""

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
    opts = {
        "quiet": True,
        "proxy": f"socks5://127.0.0.1:{BASE_PORT}",
        "force_ipv4": True,
        "no_warnings": True
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = [
            {
                "format_id": f["format_id"],
                "ext": f["ext"],
                "resolution": f.get("resolution") or f"{f.get('height','?')}p",
                "filesize": f.get("filesize"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
            }
            for f in info["formats"] if f.get("url")
        ]
        return formats, info.get("title", "video")

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

        kb = InlineKeyboardBuilder()
        for f in formats:
            if f["vcodec"] != "none" and f["acodec"] == "none":  # видео без звука
                size = f["filesize"] / 1024 / 1024 if f["filesize"] else 0
                text = f"{f['resolution']} {f['ext']}"
                if size:
                    text += f" ~{int(size)}MB"
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
        vfile, afile, final = os.path.join(tmpdir, "v.mp4"), os.path.join(tmpdir, "a.m4a"), os.path.join(tmpdir, f"{title}.mp4")
        try:
            # Получаем прямые ссылки
            vurl, aurl = None, None
            with yt_dlp.YoutubeDL({"proxy": f"socks5://127.0.0.1:{BASE_PORT}", "format": fmt, "get_url": True, "quiet": True}) as ydl:
                vurl = ydl.extract_info(url, download=False).get("url")
            for fallback in ["bestaudio", "140", "251"]:
                try:
                    with yt_dlp.YoutubeDL({"proxy": f"socks5://127.0.0.1:{BASE_PORT}", "format": fallback, "get_url": True, "quiet": True}) as ydl:
                        aurl = ydl.extract_info(url, download=False).get("url")
                        break
                except: continue
            if not vurl or not aurl:
                raise RuntimeError("Не удалось получить ссылки")
            # Качаем
            await asyncio.gather(
                download_with_tordl(vurl, vfile),
                download_with_tordl(aurl, afile)
            )
            # Мержим
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
