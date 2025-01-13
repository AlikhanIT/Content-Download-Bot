from aiogram import types
from bot.utils.log import log_action
import asyncio
import time
import aiohttp

# Функция для замера скорости интернета
async def measure_download_speed():
    url = "http://speed.hetzner.de/100MB.bin"  # Тестовый файл для замера
    start_time = time.time()

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            await response.content.read(10 * 1024 * 1024)  # Скачиваем 10 МБ

    end_time = time.time()
    duration = end_time - start_time
    speed_mbps = (10 * 8) / duration  # Переводим в мегабиты в секунду

    return round(speed_mbps, 2)

# Обработчик команды /start
async def start(message: types.Message):
    user = message.from_user
    log_action("Команда /start", f"Пользователь: {user.id} ({user.username})")

    await message.answer("Привет! Измеряю скорость интернета, подождите...")

    # Замер скорости интернета
    speed = await measure_download_speed()
    log_action("Скорость интернета", f"{speed} Mbps")

    await message.answer(f"Скорость интернета: {speed} Mbps\nОтправь мне ссылку на видео, и я помогу его скачать.")
