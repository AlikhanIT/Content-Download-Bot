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
            log_action(f"📊 Статус: {d['status'].upper()}")

            if d['status'] == 'downloading':
                speed = d.get('speed', 0) or 0
                eta = d.get('eta', 0) or 0
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded_bytes = d.get('downloaded_bytes', 0) or 0

                percent = (downloaded_bytes / total_bytes * 100) if total_bytes else 0

                log_action(f"⬇️ Скачивание: {percent:.2f}% | "
                           f"Размер: {total_bytes / (1024 * 1024):.2f} MB | "
                           f"Загружено: {downloaded_bytes / (1024 * 1024):.2f} MB | "
                           f"Скорость: {speed / (1024 * 1024):.2f} MB/s | "
                           f"Осталось: {eta}s")

            elif d['status'] == 'finished':
                log_action(f"✅ Скачивание завершено: {d.get('filename', 'Файл не указан')}")

            elif d['status'] == 'error':
                error_message = d.get('error', 'Неизвестная ошибка')
                error_code = d.get('error_code', 'Код не указан')
                error_url = d.get('url', 'URL не указан')
                exception_type = d.get('exception', 'Тип исключения не указан')

                log_action(f"❌ Ошибка загрузки:\n"
                           f"   ➡️ Сообщение: {error_message}\n"
                           f"   🆔 Код ошибки: {error_code}\n"
                           f"   🌐 URL: {error_url}\n"
                           f"   ⚠️ Тип исключения: {exception_type}")

            else:
                log_action(f"ℹ️ Дополнительный статус: {d}")

        # Словарь с соответствием качества и itag
        QUALITY_ITAG_MAP = {
            144: 17,  # 144p
            360: 18,  # 360p
            720: 22   # 720p
        }

        # Получаем itag по выбранному качеству
        itag = QUALITY_ITAG_MAP.get(quality, 18)  # По умолчанию 360p (itag 18)

        # Формируем строку формата
        format_string = f'{itag}+bestaudio/best' if use_dynamic_quality else f'{itag}+bestaudio'

        ydl_opts = {
            'limit_rate': '1M',  # Ограничение до 1 МБ/с
            'format': format_string if download_type == "video" else 'bestaudio/best',
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            'progress_hooks': [progress_hook],
            'noprogress': False,
            'retries': 30,
            'socket_timeout': 600,
            'continuedl': True,
            'cookies': COOKIES_FILE,
            'concurrent_fragment_downloads': 3,  # 3 потока
            'ratelimit': 2000000,  #
            'fragment_retries': 30,
            'verbose': True,
            'print': log_action,
            'forceipv4': True,
            'nocheckcertificate': True
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
