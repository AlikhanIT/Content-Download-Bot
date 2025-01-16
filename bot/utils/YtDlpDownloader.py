import yt_dlp
import asyncio
import os
import uuid

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
            url, download_type, quality, future = await self.queue.get()

            try:
                result = await self._download(url, download_type, quality)
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

    async def download(self, url, download_type="video", quality="480"):
        await self.start_workers()

        future = asyncio.get_event_loop().create_future()
        await self.queue.put((url, download_type, quality, future))
        return await future

    async def _download(self, url, download_type, quality):
        random_name = str(uuid.uuid4())
        output_template = os.path.join(DOWNLOAD_DIR, f"{random_name}.%(ext)s")
        final_file = None

        def progress_hook(d):
            nonlocal final_file
            if d['status'] == 'downloading':
                speed = d.get('speed') or 0
                eta = d.get('eta') or 0
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded_bytes = d.get('downloaded_bytes') or 0
                percent = (downloaded_bytes / total_bytes * 100) if total_bytes else 0
                log_action(f"⬇️ Скачивание: {percent:.2f}% | Скорость: {speed / 1024 / 1024:.2f} MB/s | Осталось: {eta}s")
            elif d['status'] == 'finished':
                final_file = d.get('filename')
                log_action(f"✅ Завершено: {final_file}")

        ydl_opts = {
            'format': f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]' if download_type == "video" else 'bestaudio/best',
            'outtmpl': output_template,
            'progress_hooks': [progress_hook],
            'noprogress': False,
            'retries': 10,
            'socket_timeout': 120,
            'continuedl': True,
            'cookies': COOKIES_FILE,
            'concurrent_fragment_downloads': 8,
            'fragment_retries': 10
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                log_action(f"🚀 Начало загрузки: {url}")
                ydl.download([url])
                if final_file and os.path.exists(final_file):
                    log_action(f"✅ Загрузка завершена: {final_file}")
                    return final_file
                else:
                    log_action("❌ Файл не найден после загрузки.")
                    return None
        except Exception as e:
            log_action(f"❌ Ошибка загрузки: {e}")
            return None
