import asyncio
import os
import threading
import time
import uuid
import subprocess
import yt_dlp
import requests
import logging
from functools import cached_property
from fake_useragent import UserAgent
from tqdm import tqdm

from bot.proxy.proxy_manager import get_available_proxy, ban_proxy
from bot.utils.log import log_action


class YtDlpDownloader:
    _instance = None
    DOWNLOAD_DIR = '/downloads'
    QUALITY_ITAG_MAP = {
        "144": "160", "240": "133", "360": "134", "480": "135",
        "720": "136", "1080": "137", "1440": "264", "2160": "266"
    }
    DEFAULT_VIDEO_ITAG = "243"
    DEFAULT_AUDIO_ITAG = "249"
    MAX_RETRIES = 5

    def __new__(cls, max_threads=8, max_queue_size=20):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize(max_threads, max_queue_size)
            cls._instance._ensure_download_dir()
        return cls._instance

    def _initialize(self, max_threads, max_queue_size):
        self.max_threads = max_threads
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self.is_running = False
        self.active_tasks = set()

    def _ensure_download_dir(self):
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        log_action(f"📂 Папка для загрузки: {self.DOWNLOAD_DIR}")

    @cached_property
    def user_agent(self):
        return UserAgent()

    async def start_workers(self):
        if not self.is_running:
            self.is_running = True
            for _ in range(self.max_threads):
                task = asyncio.create_task(self._worker())
                self.active_tasks.add(task)
                task.add_done_callback(self.active_tasks.discard)

    async def download(self, url, download_type="video", quality="480"):
        await self.start_workers()
        future = asyncio.get_event_loop().create_future()
        await self.queue.put((url, download_type, quality, future))
        return await future

    async def _worker(self):
        while True:
            url, download_type, quality, future = await self.queue.get()
            try:
                result = await self._process_download(url, download_type, quality)
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
            finally:
                self.queue.task_done()

    async def _process_download(self, url, download_type, quality):
        file_paths = await self._prepare_file_paths(download_type)
        try:
            if download_type == "audio":
                return await self._download_only_audio(url, file_paths['audio'])

            await self._download_video(url, file_paths['video'], quality)
            await self._download_audio(url, file_paths['audio'])
            return await self._merge_files(file_paths)

        finally:
            if download_type != 'audio':
                await self._cleanup_temp_files(file_paths)

    async def _prepare_file_paths(self, download_type):
        random_name = uuid.uuid4()
        base = {'output': os.path.join(self.DOWNLOAD_DIR, f"{random_name}.mp4")}
        if download_type == "audio":
            base['audio'] = os.path.join(self.DOWNLOAD_DIR, f"{random_name}.m4a")
        else:
            base.update({
                'video': os.path.join(self.DOWNLOAD_DIR, f"{random_name}_video.mp4"),
                'audio': os.path.join(self.DOWNLOAD_DIR, f"{random_name}_audio.m4a")
            })
        return base

    async def _merge_files(self, file_paths):
        log_action("🔄 Объединение видео и аудио...")
        if not os.path.exists(file_paths['video']) or not os.path.exists(file_paths['audio']):
            raise FileNotFoundError("Один из файлов для объединения отсутствует")

        command = [
            'ffmpeg', '-y',
            '-i', file_paths['video'],
            '-i', file_paths['audio'],
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-strict', 'experimental',
            file_paths['output']
        ]
        subprocess.run(command, check=True)
        log_action(f"✅ Готовый файл: {file_paths['output']}")
        return file_paths['output']

    async def _cleanup_temp_files(self, file_paths):
        for key in ['video', 'audio']:
            try:
                if os.path.exists(file_paths[key]):
                    os.remove(file_paths[key])
                    log_action(f"🧹 Удален временный файл: {file_paths[key]}")
            except Exception as e:
                log_action(f"⚠️ Ошибка при очистке: {e}")

    async def _download_only_audio(self, url, output_path):
        log_action("🎧 Скачивание только аудио")
        direct_url = await self._get_direct_url(url, self.DEFAULT_AUDIO_ITAG)
        self._download_direct(direct_url, output_path, media_type='audio')
        return output_path

    async def _download_video(self, url, output_path, quality):
        itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
        return await self._download_with_retries(url, output_path, "video", itag)

    async def _download_audio(self, url, output_path):
        return await self._download_with_retries(url, output_path, "audio", self.DEFAULT_AUDIO_ITAG)

    async def _download_with_retries(self, url, output_path, media_type, itag):
        for attempt in range(self.MAX_RETRIES):
            try:
                direct_url = await self._get_direct_url(url, itag)
                self._download_direct(direct_url, output_path, media_type)
                return output_path
            except Exception as e:
                log_action(f"❌ Попытка {attempt + 1} не удалась: {e}")
                await asyncio.sleep(2)
        raise Exception("⚠️ Все попытки скачивания исчерпаны")

    async def _get_direct_url(self, video_url, itag):
        proxy = await self._get_proxy()
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'proxy': proxy['url'],
            'user_agent': self.user_agent.random,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            for fmt in info['formats']:
                if fmt.get('format_id') == itag:
                    log_action(f"Ссылка: {fmt.get("url")}")
                    return fmt.get('url')
        raise Exception(f"Не удалось найти прямую ссылку для itag={itag}")

    import time

    def download_chunk(url, start, end, file, max_speed, pbar):
        """Скачивание одного куска с учетом ограничения скорости"""
        headers = {
            'Range': f'bytes={start}-{end}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        try:
            with requests.get(url, headers=headers, stream=True, timeout=10) as r:
                r.raise_for_status()
                chunk_size = 8192
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        start_time = time.time()

                        with file.get_lock():  # Синхронизация для многопоточности
                            file.seek(start)
                            size = file.write(chunk)
                            pbar.update(size)

                        # Ограничение скорости
                        expected_time = len(chunk) / max_speed
                        elapsed_time = time.time() - start_time
                        if elapsed_time < expected_time:
                            time.sleep(expected_time - elapsed_time)
        except Exception as e:
            log_action(f"❌ Ошибка в куске {start}-{end}: {e}")

    def _download_direct(self, url, filename, media_type, num_threads=1):
        """Скачивание файла с Range-запросами и ограничением скорости"""
        try:
            # Максимальная скорость в байтах/секунду (1 MB/s)
            MAX_SPEED = 1024 * 1024 * 5  # Можно настроить

            # Получаем размер файла
            with requests.head(url) as r:
                r.raise_for_status()
                total = int(r.headers.get('Content-Length', 0))
                if total == 0:
                    raise ValueError("Не удалось определить размер файла")

            total_mb = total / (1024 * 1024)
            log_action(f"⬇️ Начало загрузки {media_type.upper()}: {total_mb:.2f} MB — {filename}")

            # Открываем файл и резервируем место
            with open(filename, 'r+b' if num_threads > 1 else 'wb') as f:
                if num_threads > 1:
                    f.truncate(total)  # Резервируем место для многопоточности

                # Прогресс-бар
                with tqdm(total=total, unit='B', unit_scale=True, unit_divisor=1024,
                          desc=f"{media_type.upper()}") as pbar:
                    if num_threads == 1:
                        # Однопоточный режим с Range
                        chunk_size = 1024 * 1024 * 5  # 1 MB на запрос
                        downloaded = 0

                        while downloaded < total:
                            end = min(downloaded + chunk_size - 1, total - 1)
                            headers = {
                                'Range': f'bytes={downloaded}-{end}',
                                'User-Agent': 'Mozilla/5.0 ...'  # Тот же User-Agent
                            }
                            with requests.get(url, headers=headers, stream=True, timeout=10) as r:
                                r.raise_for_status()
                                for chunk in r.iter_content(chunk_size=8192):
                                    if chunk:
                                        start_time = time.time()

                                        size = f.write(chunk)
                                        downloaded += size
                                        pbar.update(size)

                                        # Логирование прогресса
                                        percent = (downloaded / total) * 100
                                        downloaded_mb = downloaded / (1024 * 1024)
                                        log_action(
                                            f"⬇️ {media_type.upper()} {percent:.2f}% "
                                            f"({downloaded_mb:.2f} MB / {total_mb:.2f} MB) — {filename}"
                                        )

                                        # Ограничение скорости
                                        expected_time = len(chunk) / MAX_SPEED
                                        elapsed_time = time.time() - start_time
                                        if elapsed_time < expected_time:
                                            time.sleep(expected_time - elapsed_time)
                    else:
                        # Многопоточный режим
                        chunk_size = total // num_threads
                        threads = []

                        for i in range(num_threads):
                            start = i * chunk_size
                            end = start + chunk_size - 1 if i < num_threads - 1 else total - 1
                            t = threading.Thread(target=self.download_chunk, args=(url, start, end, f, MAX_SPEED, pbar))
                            threads.append(t)
                            t.start()

                        for t in threads:
                            t.join()

            log_action(f"✅ Скачивание завершено: {filename}")
        except Exception as e:
            log_action(f"❌ Ошибка при скачивании {filename}: {e}")

    async def _get_proxy(self):
        proxy = {'ip': '127.0.0.1', 'port': '9050'}
        proxy_url = f"socks5://{proxy['ip']}:{proxy['port']}"
        log_action(f"🛡 Используется прокси для получения ссылки: {proxy_url}")
        return {'url': proxy_url, 'key': f"{proxy['ip']}:{proxy['port']}"}

    def _handle_progress(self, d):
        status = d.get('status')
        if status == 'downloading':
            speed = d.get('speed', 0)
            eta = d.get('eta', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            done = d.get('downloaded_bytes', 0)
            percent = (done / total * 100) if total else 0

            log_action(
                f"⬇️ Скачивание: {percent:.2f}% | Размер: {total / 2**20:.2f} MB | "
                f"Загружено: {done / 2**20:.2f} MB | Скорость: {speed / 2**20:.2f} MB/s | Осталось: {eta}s"
            )
        elif status == 'finished':
            log_action(f"✅ Завершено: {d.get('filename', 'Файл не указан')}")
        elif status == 'error':
            log_action(f"❌ Ошибка: {d.get('error', 'Неизвестная ошибка')}")
        else:
            log_action(f"ℹ️ Статус: {d}")
