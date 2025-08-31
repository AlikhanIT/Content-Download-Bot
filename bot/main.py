import os
import asyncio
import tempfile
import uuid
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
import logging
import sys
import platform

# ================== CONFIG ================== #
API_TOKEN = os.getenv("API_TOKEN")
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://localhost:8081")

BASE_PORT = 9050

session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL), timeout=1800)
bot = Bot(token=API_TOKEN, session=session)
dp = Dispatcher()



# ================== ЛОГГЕР ================== #
logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)  # показываем все уровни (DEBUG, INFO, WARNING, ERROR)

handler = logging.StreamHandler(sys.stdout)  # прямой вывод в консоль (stdout)
formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)

def log(msg: str, level="info"):
    if level == "debug":
        logger.debug(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.info(msg)


async def run_cmd(cmd: list[str], cwd=None):
    log(f"▶ Запуск команды: {' '.join(cmd)} (cwd={cwd or os.getcwd()})")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    # Читаем STDOUT построчно и сразу логируем (живой прогресс tor-dl)
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode().strip()
        if text:
            log(f"[tor-dl] {text}")

    # Читаем STDERR в конце
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode().strip()
        if text:
            log(f"[STDERR] {text}")

    await proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"Команда завершилась с кодом {proc.returncode}")
    log("✔ Команда завершена успешно")
    return ""


async def download_with_tordl(url: str, out_file: str):
    """Скачивание через tor-dl на 9050"""
    fname = os.path.basename(out_file)
    workdir = os.path.dirname(out_file)

    log(f"▶ Начало скачивания: {url}")
    log(f"▶ Выходной файл: {out_file}")
    IS_WINDOWS = platform.system().lower().startswith("win")

    # Если Windows → берем tor-dl.exe из текущей папки
    # Если Linux/Mac → берем tor-dl (без расширения)
    if IS_WINDOWS:
        TOR_DL_PATH = os.path.join(os.getcwd(), "tor-dl.exe")
    else:
        TOR_DL_PATH = os.path.join(os.getcwd(), "tor-dl")
    cmd = [
        TOR_DL_PATH,
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

    log(f"✔ Файл скачан: {out_file}")


# ================== HANDLERS ================== #
@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    log(f"Получена команда /start от {msg.from_user.id}")
    await msg.answer("Отправь прямую ссылку на файл (например YouTube `videoplayback?...itag=18`) 🎬")


@dp.message(F.text.regexp(r"https?://\S+"))
async def handle_link(msg: types.Message):
    url = msg.text.strip()
    log(f"Получена ссылка от {msg.from_user.id}: {url}")

    await msg.answer("⚡ Скачиваю через Tor...")

    with tempfile.TemporaryDirectory() as tmpdir:
        guid_name = f"{uuid.uuid4().hex}.mp4"
        out_file = os.path.join(tmpdir, guid_name)
        log(f"Сгенерировано имя файла: {guid_name}")

        try:
            await download_with_tordl(url, out_file)

            try:
                await msg.answer_video(types.FSInputFile(out_file), caption=guid_name)
                log("Файл отправлен как video")
            except Exception as e:
                log(f"⚠ Ошибка при отправке video: {e}, пробую как document")
                await msg.answer_document(types.FSInputFile(out_file), caption=guid_name)
                log("Файл отправлен как document")

            await msg.answer("✅ Готово!")
            log("Отправка пользователю завершена успешно")

        except Exception as e:
            err_msg = f"Ошибка загрузки: {e}"
            log(f"❌ {err_msg}")
            await msg.answer(f"❌ {err_msg}")


# ================== MAIN ================== #
async def main():
    me = await bot.get_me()
    log(f"Бот @{me.username} запущен ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
