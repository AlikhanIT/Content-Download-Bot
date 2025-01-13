import asyncio
import os
import shutil
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, BufferedInputFile
from bot.config import bot
from bot.database.mongo import save_to_cache, get_from_cache, remove_from_cache
from bot.utils.YtDlpDownloader import YtDlpDownloader
from bot.utils.log import log_action
from bot.utils.video_info import get_video_info, get_thumbnail_bytes  # Импортируем новый метод

downloading_status = {}
max_concurrent_downloads = 10  # Максимальное количество одновременных загрузок
semaphore_downloads = asyncio.Semaphore(max_concurrent_downloads)  # Ограничение на количество одновременных загрузок
downloader = YtDlpDownloader(max_threads=max_concurrent_downloads)

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
            log_action("Отправка файла с кэша")
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
                log_action("Отправка файла и сохранение в кэш")
                output_file = await downloader.download(url, download_type, quality)
                thumbnail_bytes = await get_thumbnail_bytes(thumbnail_url) if thumbnail_url else None

                if not output_file or not os.path.exists(output_file):
                    await bot.send_message(user_id, "Ошибка скачивания.")
                    return

                file_to_send = FSInputFile(output_file)
                if thumbnail_bytes:
                    thumbnail_to_send = BufferedInputFile(thumbnail_bytes.read(), filename="thumbnail.jpg")
                else:
                    thumbnail_to_send = None

                if download_type == "video":
                    log_action(f"✅ Отправка началась: {output_file}")
                    message = await bot.send_video(
                        user_id,
                        video=file_to_send,
                        caption=f"Ваше видео готово: {title}",
                        supports_streaming=True,
                        thumbnail=thumbnail_to_send  # Передаём превью как вложение
                    )
                    log_action(f"✅ Отправка закончилась: {output_file}")
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
