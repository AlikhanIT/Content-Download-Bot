import asyncio
import contextlib
import os
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, BufferedInputFile
from bot.config import bot
from bot.database.mongo import save_to_cache, get_from_cache, remove_from_cache
from bot.utils.YtDlpDownloader import YtDlpDownloader
from bot.utils.log import log_action
from bot.utils.video_info import get_video_info, get_thumbnail_bytes, get_video_resolution

# Статусы и лимит на загрузки
downloading_status = {}
max_concurrent_downloads = 10
semaphore_downloads = asyncio.Semaphore(max_concurrent_downloads)
downloader = YtDlpDownloader(max_threads=max_concurrent_downloads)

async def update_progress(user_id, msg, total_size=100):
    import random
    import time

    percent = 0
    start_time = time.time()

    while percent < 100:
        if downloading_status.get(user_id) == "cancelled":
            return

        percent += random.randint(1, 10)
        percent = min(percent, 100)

        eta = int((100 - percent) * 0.5)  # Примерно
        bar = "▓" * (percent // 10) + "░" * (10 - percent // 10)

        text = f"🔄 Загрузка: {bar} {percent}%\n⏱ Осталось ~{eta} сек."
        try:
            await bot.edit_message_text(text, msg.chat.id, msg.message_id, reply_markup=msg.reply_markup)
        except:
            pass

        await asyncio.sleep(2)

async def download_and_send(user_id, url, download_type, quality, progress_message=None):
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
            log_action("🚀 Отправка с кэша:")
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
                    await remove_from_cache(video_id, download_type, quality)
                else:
                    await bot.send_message(user_id, f"Произошла ошибка: {e}")
                    downloading_status.pop(user_id, None)
                return

        async def download_all():
            output_file = None
            thumbnail_to_send = None
            width = height = None

            try:
                log_action("🚀 Начало загрузки и превью")

                # Скачивание файла и превью параллельно
                download_task = downloader.download(url, download_type, quality)
                progress_updater = asyncio.create_task(update_progress(user_id, progress_message))
                thumbnail_task = get_thumbnail_bytes(thumbnail_url) if thumbnail_url and download_type == "video" else None

                tasks = [download_task]
                if thumbnail_task is not None:
                    tasks.append(thumbnail_task)

                results = await asyncio.gather(*tasks, return_exceptions=True)
                output_file = results[0]
                thumbnail_bytes = results[1] if download_type == "video" else None

                if isinstance(output_file, Exception):
                    downloading_status.pop(user_id, None)
                    raise output_file
                if not output_file or not os.path.exists(output_file):
                    await bot.send_message(user_id, "Ошибка скачивания.")
                    return

                if thumbnail_bytes:
                    thumbnail_to_send = BufferedInputFile(thumbnail_bytes.read(), filename="thumbnail.jpg")
                    width, height = await get_video_resolution(output_file)

                file_to_send = FSInputFile(output_file)

                if download_type == "video":
                    message = await bot.send_video(
                        user_id,
                        video=file_to_send,
                        caption=f"Ваше видео готово: {title}",
                        supports_streaming=True,
                        thumbnail=thumbnail_to_send,
                        width=width,
                        height=height
                    )
                    await save_to_cache(video_id, download_type, quality, message.video.file_id)
                else:
                    message = await bot.send_audio(
                        user_id,
                        audio=file_to_send,
                        caption=f"Ваше аудио готово: {title}"
                    )
                    await save_to_cache(video_id, download_type, quality, message.audio.file_id)

            finally:
                if isinstance(output_file, Exception):
                    raise output_file  # или логируй
                if output_file and os.path.exists(output_file):
                    os.remove(output_file)
                downloading_status.pop(user_id, None)
                log_action(f"Файл отправен: {output_file}")

                progress_updater.cancel()
                with contextlib.suppress(Exception):
                    await bot.edit_message_text("✅ Загрузка завершена.", user_id, progress_message.message_id)

        await download_all()
