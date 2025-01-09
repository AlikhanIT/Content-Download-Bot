import shutil  # Импортируем модуль shutil
import os
import uuid
import asyncio
from bot.utils.video_info import get_video_info
from bot.utils.log import log_action
from bot.database.mongo import save_to_cache, get_from_cache
from bot.config import bot, semaphore
import subprocess
from aiogram.types import FSInputFile  # Добавляем этот импорт

downloading_status = {}  # Хранилище статуса загрузок по пользователям

async def download_media_async(url, download_type="video", quality="720", output_dir="downloads"):
    os.makedirs(output_dir, exist_ok=True)
    random_name = str(uuid.uuid4())
    output_file = os.path.join(output_dir, f"{random_name}.mp4" if download_type == "video" else f"{random_name}.mp3")

    # Указываем формат для `yt-dlp`
    if download_type == "video":
        format_option = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]"
    else:
        format_option = "bestaudio/best"

    command = [
        "yt-dlp",
        "-f", format_option,
        "-o", output_file,
        url
    ]

    if download_type == "audio":
        command += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "192"]

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

async def download_and_send(user_id, url, download_type, quality):
    if downloading_status.get(user_id):
        await bot.send_message(user_id, "Видео уже скачивается. Пожалуйста, подождите.")
        return

    downloading_status[user_id] = True

    async with semaphore:
        video_id, title, _ = await get_video_info(url)
        if not video_id:
            await bot.send_message(user_id, "Не удалось извлечь информацию о видео.")
            downloading_status.pop(user_id, None)
            return

        cached_file_id = await get_from_cache(video_id, download_type, quality)
        if cached_file_id:
            if download_type == "video":
                await bot.send_video(user_id, video=cached_file_id, caption=f"Ваше видео готово: {title}")
            else:
                await bot.send_audio(user_id, audio=cached_file_id, caption=f"Ваше аудио готово: {title}")
            downloading_status.pop(user_id, None)
            return

        output_file = await download_media_async(url, download_type, quality)
        if not output_file or not os.path.exists(output_file):
            await bot.send_message(user_id, "Ошибка скачивания.")
            absolute_path = os.path.abspath(output_file)
            log_action(absolute_path)
            log_action("Ошибка: файл отсутствует", f"Путь: {output_file}")
            return

        if output_file:
            file_to_send = FSInputFile(output_file)
            if download_type == "video":
                message = await bot.send_video(user_id, video=file_to_send, caption=f"Ваше видео готово: {title}")
                await save_to_cache(video_id, download_type, quality, message.video.file_id)
            else:
                message = await bot.send_audio(user_id, audio=file_to_send, caption=f"Ваше аудио готово: {title}")
                await save_to_cache(video_id, download_type, quality, message.audio.file_id)
            os.remove(output_file)

        downloading_status.pop(user_id, None)

def check_ffmpeg_installed():
    if not shutil.which("ffmpeg"):
        raise EnvironmentError("FFmpeg не установлен. Установите его и добавьте в PATH.")
