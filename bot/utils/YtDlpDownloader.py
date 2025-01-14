import asyncio
import os
import subprocess
import uuid
from bot.utils.log import log_action
from bot.utils.video_info import add_range_to_url, get_clen


class YtDlpDownloader:
    _instance = None

    def __new__(cls, max_threads=8, max_queue_size=20):
        if cls._instance is None:
            cls._instance = super(YtDlpDownloader, cls).__new__(cls)
            cls._instance.max_threads = max_threads
            cls._instance.queue = asyncio.Queue(maxsize=max_queue_size)  # Очередь задач
            cls._instance.is_running = False
        return cls._instance

    async def _worker(self):
        while True:
            url, download_type, quality, output_dir, future = await self.queue.get()

            try:
                result = await self._download(url, download_type, quality, output_dir)
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

    async def download(self, url, download_type="video", quality="720", output_dir="downloads"):
        await self.start_workers()

        future = asyncio.get_event_loop().create_future()
        await self.queue.put((url, download_type, quality, output_dir, future))
        return await future

    async def _download(self, url, download_type, quality, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        random_name = str(uuid.uuid4())
        output_file = os.path.join(output_dir,
                                   f"{random_name}.mp4" if download_type == "video" else f"{random_name}.mp3")

        # ⚡ Получаем метаданные для извлечения 'clen'
        clen = await get_clen(url)

        # 🔗 Модифицируем URL с добавлением range
        ranged_url = add_range_to_url(url, clen) if clen else url

        format_option = (
            f"bestvideo[height={quality}]+bestaudio[abr<=128]/best[height={quality}]"
            if download_type == "video"
            else "bestaudio[abr<=128]/best"
        )

        command = [
            "yt-dlp",
            "-f", format_option,
            "-N", "8",  # 🚀 8 параллельных потоков для ускорения загрузки
            "--merge-output-format", "mp4",
            "-o", output_file,
            "--socket-timeout", "120",  # Увеличенный таймаут
            "--retries", "10",  # Увеличенные попытки
            "--extractor-args", "youtube:po_token=android+XXX",
            "--no-check-certificate",  # Игнор сертификатов
            "--downloader", "aria2c",  # 🚀 Использование aria2c как загрузчика
            "--downloader-args",
            "aria2c:--continue --max-concurrent-downloads=30 --max-connection-per-server=16 --split=30 --min-split-size=1M",
            ranged_url  # 🚀 Ссылка с range
        ]

        log_action(f"✅ Скачивание началось: {output_file}")
        log_action(f"✅ Скачивание началось: {ranged_url}")
        process = await asyncio.create_subprocess_exec(*command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            log_action(f"✅ Скачивание завершено: {output_file}")
            return output_file
        else:
            log_action(f"❌ Ошибка скачивания: {stderr.decode()}")
            return None