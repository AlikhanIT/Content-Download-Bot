import asyncio
import os
import threading
import time
import uuid
import subprocess

import aiofiles
import aiohttp
import yt_dlp
import requests
from functools import cached_property

from aiohttp import ClientError
from fake_useragent import UserAgent
from tqdm import tqdm

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

            # Параллельное скачивание видео и аудио
            await asyncio.gather(
                asyncio.create_task(self._download_video(url, file_paths['video'], quality)),
                asyncio.create_task(self._download_audio(url, file_paths['audio']))
            )

            # После завершения — объединяем
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

    # async def _merge_files(self, file_paths):
    #     log_action("🔄 Объединение видео и аудио...")
    #     if not os.path.exists(file_paths['video']) or not os.path.exists(file_paths['audio']):
    #         raise FileNotFoundError("Один из файлов для объединения отсутствует")
    #
    #     command = [
    #         'ffmpeg', '-y',
    #         '-i', file_paths['video'],
    #         '-i', file_paths['audio'],
    #         '-c:v', 'copy',
    #         '-c:a', 'aac',
    #         '-strict', 'experimental',
    #         file_paths['output']
    #     ]
    #     subprocess.run(command, check=True)
    #     log_action(f"✅ Готовый файл: {file_paths['output']}")
    #     return file_paths['output']

    async def _merge_files(self, file_paths):
        """Асинхронное объединение видео и аудио с помощью MP4Box"""
        log_action("🔄 Объединение видео и аудио...")

        # Проверка существования файлов
        if not os.path.exists(file_paths['video']) or not os.path.exists(file_paths['audio']):
            raise FileNotFoundError("Один из файлов для объединения отсутствует")

        # Команда для MP4Box
        command = [
            'MP4Box',
            '-add', file_paths['video'],
            '-add', file_paths['audio'],
            file_paths['output']
        ]

        # Асинхронный запуск процесса
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Ожидание завершения и получение вывода
        stdout, stderr = await process.communicate()

        # Проверка результата
        if process.returncode == 0:
            log_action(f"✅ Готовый файл: {file_paths['output']}")
            return file_paths['output']
        else:
            error_message = stderr.decode() if stderr else "Неизвестная ошибка MP4Box"
            raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)

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
        await self._download_direct(direct_url, output_path, media_type='audio')
        return output_path

    async def _download_video(self, url, output_path, quality):
        itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
        return await self._download_with_retries(url, output_path, "video", itag)

    async def _download_audio(self, url, output_path):
        return await self._download_with_retries(url, output_path, "audio", self.DEFAULT_AUDIO_ITAG)

    async def _download_with_retries(self, url, output_path, media_type, itag):
        loop = asyncio.get_running_loop()
        for attempt in range(self.MAX_RETRIES):
            try:
                direct_url = await self._get_direct_url(url, itag)

                await self._download_direct(direct_url, output_path, media_type)

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

    def download_chunk(url, start, end, file, max_speed, pbar):
        """Скачивание одного куска с учетом ограничения скорости"""
        headers = {
            'Range': f'bytes={start}-{end}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': '*/*',
            'Referer': 'https://www.youtube.com/'
        }
        try:
            with requests.get(url, headers=headers, stream=True, timeout=10) as r:
                r.raise_for_status()
                chunk_size = 32768  # 32 KB
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

    async def _download_direct(self, url, filename, media_type, num_parts=4):
        """Асинхронное скачивание файла с поддержкой Range, редиректов и многозадачности"""
        try:
            MAX_SPEED = 1024 * 1024 * 5  # 5 MB/s
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Accept': '*/*',
                'Referer': 'https://www.youtube.com/'
            }

            timeout = aiohttp.ClientTimeout(total=600)

            # 🌀 Ручная обработка редиректов до финального URL
            redirect_count = 0
            max_redirects = 10
            current_url = url
            total = 0

            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                while redirect_count < max_redirects:
                    async with session.head(current_url, allow_redirects=False) as r:
                        if r.status in (301, 302, 303, 307, 308):
                            location = r.headers.get('Location')
                            if not location:
                                raise ValueError("Нет заголовка Location при редиректе")
                            log_action(f"🔁 Редирект #{redirect_count + 1}: {location}")
                            current_url = location
                            redirect_count += 1
                            continue

                        r.raise_for_status()
                        total = int(r.headers.get('Content-Length', 0))
                        if total == 0:
                            raise ValueError("Не удалось определить размер файла")
                        break

            if redirect_count >= max_redirects:
                raise ValueError(f"Превышено число редиректов ({max_redirects})")

            total_mb = total / (1024 * 1024)
            log_action(f"⬇️ Начало загрузки {media_type.upper()}: {total_mb:.2f} MB — {filename}")

            # 🔧 Создание файла с нужным размером
            async with aiofiles.open(filename, 'wb') as f:
                await f.seek(total - 1)
                await f.write(b'\0')

            pbar = tqdm(total=total, unit='B', unit_scale=True, unit_divisor=1024, desc=media_type.upper())

            async def download_range(start, end):
                range_headers = headers.copy()
                range_headers['Range'] = f'bytes={start}-{end}'

                async with aiohttp.ClientSession(headers=range_headers, timeout=timeout) as part_session:
                    async with part_session.get(current_url) as resp:
                        resp.raise_for_status()
                        async with aiofiles.open(filename, 'r+b') as f:
                            await f.seek(start)
                            async for chunk in resp.content.iter_chunked(1024 * 128):
                                if chunk:
                                    await f.write(chunk)
                                    pbar.update(len(chunk))

            # 🎯 Создание задач для загрузки чанков
            part_size = total // num_parts
            tasks = []
            for i in range(num_parts):
                start = i * part_size
                end = total - 1 if i == num_parts - 1 else (start + part_size - 1)
                tasks.append(asyncio.create_task(download_range(start, end)))

            await asyncio.gather(*tasks)
            pbar.close()
            log_action(f"✅ Скачивание завершено: {filename}")

        except ClientError as e:
            log_action(f"❌ HTTP ошибка при скачивании {filename}: {e}")
        except Exception as e:
            log_action(f"❌ Ошибка при скачивании {filename}: {e}")

    async def _get_proxy(self):
        proxy = {'ip': '127.0.0.1', 'port': '9150'}
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
