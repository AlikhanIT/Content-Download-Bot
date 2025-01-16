import yt_dlp
import asyncio
import os
import uuid
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
        output_file = os.path.join(DOWNLOAD_DIR, f"{random_name}.mp4" if download_type == "video" else f"{random_name}.mp3")

        ydl_opts = {
            'format': 'bestvideo[height<=480]+bestaudio/best[height<=480]',  # Скачивает видео в 480p или ниже
            'progress_hooks': [
                lambda d: logger.info(
                    f"{d['status'].upper()}: {d.get('filename', '')} - {d.get('info_dict', {}).get('title', '')}")
            ],
            'logger': None,
            'outtmpl': output_file,  # Путь сохранения
            'progress': True,  # Показывает прогресс
            'retries': 5,  # Количество попыток при ошибке
            'socket_timeout': 60,  # Тайм-аут соединения увеличен до 60 секунд
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                log_action(f"🚀 Начало загрузки: {url}")
                ydl.download([url])
                log_action(f"✅ Загрузка завершена: {output_file}")
                return output_file
        except Exception as e:
            log_action(f"❌ Ошибка загрузки: {e}")
            return None
