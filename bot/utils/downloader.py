import asyncio
import os
import shutil
import subprocess
import uuid

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, BufferedInputFile

from bot.config import bot
from bot.database.mongo import save_to_cache, get_from_cache, remove_from_cache
from bot.utils.log import log_action
from bot.utils.video_info import get_video_info, get_thumbnail_bytes  # Импортируем новый метод

downloading_status = {}
max_concurrent_downloads = 10  # Максимальное количество одновременных загрузок
semaphore_downloads = asyncio.Semaphore(max_concurrent_downloads)  # Ограничение на количество одновременных загрузок

# Асинхронная загрузка с использованием yt-dlp для получения ссылки
async def download_media_async(url, download_type="video", quality="720", output_dir="downloads"):
    os.makedirs(output_dir, exist_ok=True)
    random_name = str(uuid.uuid4())
    output_file = os.path.join(output_dir, f"{random_name}.mp4" if download_type == "video" else f"{random_name}.mp3")

    if download_type == "video":
        # Скачивание видео с указанным качеством и средним аудио
        format_option = f"bestvideo[height={quality}]+bestaudio[abr<=128]/best[height={quality}]"
    else:
        # Скачивание только аудио в среднем качестве
        format_option = "bestaudio[abr<=128]/best"

    command = [
        "yt-dlp",
        "-f", format_option,
        "--merge-output-format", "mp4",  # Принудительное сохранение в mp4
        "-o", output_file,
        url
    ]

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


# Асинхронная загрузка и отправка файлов с превью
async def download_and_send(user_id, url, download_type, quality):
    if downloading_status.get(user_id):
        await bot.send_message(user_id, "Видео уже скачивается. Пожалуйста, подождите.")
        return

    downloading_status[user_id] = True

    async with semaphore_downloads:
        video_id, title, thumbnail_url = await get_video_info(url)
        if not video_id:
            await bot.send_message(user_id, "Не удалось извлечь информацию о видео.")
            downloading_status.pop(user_id, None)
            return

        cached_file_id = await get_from_cache(video_id, download_type, quality)
        if cached_file_id:
            try:
                if download_type == "video":
                    await bot.send_video(user_id, video=cached_file_id, caption=f"Ваше видео готово: {title}", supports_streaming=True)
                else:
                    await bot.send_audio(user_id, audio=cached_file_id, caption=f"Ваше аудио готово: {title}")
                downloading_status.pop(user_id, None)
                return
            except TelegramBadRequest as e:
                if "wrong file identifier" in str(e):
                    await bot.send_message(user_id, "Файл повреждён или удалён. Скачиваю заново...")
                    await remove_from_cache(video_id, download_type, quality)  # Удаляем некорректный ID из кэша
                else:
                    await bot.send_message(user_id, f"Произошла ошибка: {e}")
                    downloading_status.pop(user_id, None)
                    return

        async def download_and_send_file():
            try:
                output_file = await download_media_async(url, download_type, quality)
                thumbnail_bytes = await get_thumbnail_bytes(thumbnail_url) if thumbnail_url else None

                if not output_file or not os.path.exists(output_file):
                    await bot.send_message(user_id, "Ошибка скачивания.")
                    return

                file_to_send = FSInputFile(output_file)
                thumbnail_to_send = BufferedInputFile(thumbnail_bytes.read(),
                                                      filename="thumbnail.jpg") if thumbnail_bytes else None

                if download_type == "video":
                    message = await bot.send_video(
                        user_id,
                        video=file_to_send,
                        caption=f"Ваше видео готово: {title}",
                        supports_streaming=True,
                        thumbnail=thumbnail_to_send
                    )
                    await save_to_cache(video_id, download_type, quality, message.video.file_id)
                else:
                    message = await bot.send_audio(user_id, audio=file_to_send, caption=f"Ваше аудио готово: {title}")
                    await save_to_cache(video_id, download_type, quality, message.audio.file_id)

            finally:
                if output_file and os.path.exists(output_file):
                    os.remove(output_file)
                downloading_status.pop(user_id, None)

        await download_and_send_file()

def check_ffmpeg_installed():
    if not shutil.which("ffmpeg"):
        raise EnvironmentError("FFmpeg не установлен. Установите его и добавьте в PATH.")
