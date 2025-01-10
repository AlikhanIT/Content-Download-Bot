import asyncio
import os
import shutil
import subprocess
import uuid
from aiogram.types import FSInputFile

from bot.config import bot
from bot.database.mongo import save_to_cache, get_from_cache
from bot.utils.log import log_action
from bot.utils.video_info import get_video_info  # Импортируем новый метод

downloading_status = {}
max_concurrent_downloads = 10  # Максимальное количество одновременных загрузок
semaphore_downloads = asyncio.Semaphore(max_concurrent_downloads)  # Ограничение на количество одновременных загрузок

# Асинхронная загрузка с использованием yt-dlp для получения ссылки
async def download_media_async(url, download_type="video", quality="720", output_dir="downloads"):
    os.makedirs(output_dir, exist_ok=True)
    random_name = str(uuid.uuid4())
    output_file = os.path.join(output_dir, f"{random_name}.mp4" if download_type == "video" else f"{random_name}.mp3")

    # Установим битрейт в зависимости от качества
    if quality == "720":
        bit_rate = "2M"  # 2 Mbps для 720p
    elif quality == "1080":
        bit_rate = "5M"  # 5 Mbps для 1080p
    elif quality == "1440":
        bit_rate = "10M"  # 10 Mbps для 1440p
    elif quality == "2160":
        bit_rate = "20M"  # 20 Mbps для 4K
    else:
        bit_rate = "2M"  # Для других случаев (по умолчанию 720p)

    # Формат видео с кодеком H.264 и битрейтом
    format_option = f"bestvideo[height<={quality}][ext=mp4][vcodec=libx264]+bestaudio[ext=m4a]/best[ext=mp4]"
    command = [
        "yt-dlp",
        "-f", format_option,
        "--recode-video", "mp4",  # Перекодировка в mp4, если необходимо
        "-o", output_file,
        url
    ]
    command += ["--postprocessor-args", f"-b:v {bit_rate}"]  # Устанавливаем битрейт для видео

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode == 0:
        log_action("Скачивание завершено", f"Файл: {output_file}")
        return output_file
    else:
        log_action("Ошибка скачивания", stderr.decode())
        return None

# Асинхронная загрузка и отправка файлов
async def download_and_send(user_id, url, download_type, quality):
    if downloading_status.get(user_id):
        await bot.send_message(user_id, "Видео уже скачивается. Пожалуйста, подождите.")
        return

    downloading_status[user_id] = True

    # Используем семафор для ограничений на количество одновременных задач
    async with semaphore_downloads:
        video_id, title, _ = await get_video_info(url)
        if not video_id:
            await bot.send_message(user_id, "Не удалось извлечь информацию о видео.")
            downloading_status.pop(user_id, None)
            return

        cached_file_id = await get_from_cache(video_id, download_type, quality)
        if cached_file_id:
            if download_type == "video":
                await bot.send_video(user_id, video=cached_file_id, caption=f"Ваше видео готово: {title}", supports_streaming=True)
            else:
                await bot.send_audio(user_id, audio=cached_file_id, caption=f"Ваше аудио готово: {title}")
            downloading_status.pop(user_id, None)
            return

        # Параллельная загрузка и отправка файла
        async def download_and_send_file():
            output_file = await download_media_async(url, download_type, quality)
            if not output_file or not os.path.exists(output_file):
                await bot.send_message(user_id, "Ошибка скачивания.")
                log_action("Ошибка: файл отсутствует", f"Путь: {output_file}")
                downloading_status.pop(user_id, None)
                return

            file_to_send = FSInputFile(output_file)
            if download_type == "video":
                # Отправка видео с поддержкой потоковой передачи
                message = await bot.send_video(user_id, video=file_to_send, caption=f"Ваше видео готово: {title}", supports_streaming=True)
                await save_to_cache(video_id, download_type, quality, message.video.file_id)
            else:
                message = await bot.send_audio(user_id, audio=file_to_send, caption=f"Ваше аудио готово: {title}")
                await save_to_cache(video_id, download_type, quality, message.audio.file_id)
            os.remove(output_file)

            downloading_status.pop(user_id, None)

        # Запускаем параллельно с ограничением количества задач
        await download_and_send_file()

def check_ffmpeg_installed():
    if not shutil.which("ffmpeg"):
        raise EnvironmentError("FFmpeg не установлен. Установите его и добавьте в PATH.")
