import yt_dlp
import asyncio
import os
import uuid
import glob

from bot.config import COOKIES_FILE
from bot.utils.log import log_action

# Проверка существования папки /downloads
DOWNLOAD_DIR = '/downloads'
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)
    log_action(f"📂 Создана папка для загрузки: {DOWNLOAD_DIR}")

class YtDlpDownloader:
    _instance = None

    def __new__(cls, max_threads=8, max_queue_size=20):
        if cls._instance is None:
            cls._instance = super(YtDlpDownloader, cls).__new__(cls)
            cls._instance.max_threads = max_threads
            cls._instance.queue = asyncio.Queue(maxsize=max_queue_size)
            cls._instance.is_running = False
        return cls._instance

    async def _worker(self):
        while True:
            url, download_type, quality, use_dynamic_quality, future = await self.queue.get()

            try:
                result = await self._download(url, download_type, quality, use_dynamic_quality)
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
            finally:
                self.queue.task_done()

    async def start_workers(self):
        if not self.is_running:
            self.is_running = True
            for _ in range(self.max_threads):
                asyncio.create_task(self._worker())

    async def download(self, url, download_type="video", quality="480", use_dynamic_quality=False):
        await self.start_workers()

        future = asyncio.get_event_loop().create_future()
        await self.queue.put((url, download_type, quality, use_dynamic_quality, future))
        return await future

    async def _download(self, url, download_type, quality, use_dynamic_quality):
        random_name = str(uuid.uuid4())
        output_template = os.path.join(DOWNLOAD_DIR, f"{random_name}.mp4")

        def progress_hook(d):
            log_action(f"📊 Полный лог: {d}")
            if d['status'] == 'downloading':
                speed = d.get('speed') or 0
                eta = d.get('eta') or 0
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
                downloaded_bytes = d.get('downloaded_bytes') or 0
                percent = (downloaded_bytes / total_bytes * 100)
                log_action(f"⬇️ Скачивание: {percent:.2f}% | Размер: {total_bytes / (1024 * 1024):.2f} MB | Загружено: {downloaded_bytes / (1024 * 1024):.2f} MB | Скорость: {speed / 1024 / 1024:.2f} MB/s | Осталось: {eta}s")
            elif d['status'] == 'finished':
                log_action(f"✅ Скачивание завершено: {d.get('filename', 'Файл не указан')}")
            elif d['status'] == 'error':
                log_action(f"❌ Ошибка загрузки: {d.get('error', 'Неизвестная ошибка')}")

        format_string = 'bestvideo+bestaudio/best' if use_dynamic_quality else f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]'

        ydl_opts = {
            'format': format_string if download_type == "video" else 'bestaudio/best',
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            'progress_hooks': [progress_hook],
            'noprogress': False,
            'retries': 10,
            'socket_timeout': 120,
            'continuedl': True,
            'cookies': COOKIES_FILE,
            'concurrent_fragment_downloads': 8,
            'fragment_retries': 10,
            'verbose': True,
            'print': log_action,
            'downloader': 'aria2c',  # Добавлен многопоточный загрузчик
            'downloader_args': {
                'aria2c': '--split=8 --max-connection-per-server=8 --min-split-size=1M'
            }
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                log_action(f"🚀 Начало загрузки: {url}")
                ydl.download([url])
                downloaded_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{random_name}.mp4"))
                if downloaded_files:
                    final_file = downloaded_files[0]
                    log_action(f"✅ Итоговый файл: {final_file}")
                    return final_file
                else:
                    log_action("❌ Итоговый файл не найден после загрузки.")
                    return None
        except Exception as e:
            log_action(f"❌ Ошибка загрузки: {e}")
            return None
