import asyncio
import json
import os
import time
import uuid
import subprocess

import aiofiles
import aiohttp
import heapq
import requests
from functools import cached_property

from aiohttp import ClientError
from aiohttp_socks import ProxyConnector
from fake_useragent import UserAgent
from tqdm import tqdm

from bot.utils.log import log_action
from bot.utils.video_info import get_video_info_with_cache, extract_url_from_info


class YtDlpDownloader:
    _instance = None
    DOWNLOAD_DIR = '/downloads'
    QUALITY_ITAG_MAP = {
        "144": "160", "240": "133", "360": "134", "480": "135",
        "720": "136", "1080": "137", "1440": "264", "2160": "266"
    }
    DEFAULT_VIDEO_ITAG = "243"
    DEFAULT_AUDIO_ITAG = "249"
    MAX_RETRIES = 10

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
            proxy_ports = [9050, 9052, 9054, 9056, 9058, 9060, 9062, 9064, 9066, 9068, 9070, 9072, 9074, 9076, 9078]

            info = await get_video_info_with_cache(url)

            if download_type == "audio":
                audio_itags = ["249", "250", "251", "140"]
                direct_audio_url = await extract_url_from_info(info, audio_itags)
                await self._download_direct(direct_audio_url, file_paths['audio'], media_type='audio', proxy_ports=proxy_ports)
                return file_paths['audio']

            video_itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)

            # 🎯 Получение ссылок из уже кэшированного info
            video_url_task = asyncio.create_task(extract_url_from_info(info, [video_itag]))
            audio_url_task = asyncio.create_task(extract_url_from_info(info, ["249", "250", "251", "140"]))

            results = await asyncio.gather(video_url_task, audio_url_task, return_exceptions=True)

            if any(isinstance(r, Exception) for r in results):
                video_url_task.cancel()
                audio_url_task.cancel()
                for r in results:
                    if isinstance(r, Exception):
                        raise r

            direct_video_url, direct_audio_url = results

            video_task = asyncio.create_task(
                self._download_direct(direct_video_url, file_paths['video'], media_type='video', proxy_ports=proxy_ports)
            )
            audio_task = asyncio.create_task(
                self._download_direct(direct_audio_url, file_paths['audio'], media_type='audio', proxy_ports=proxy_ports)
            )

            results = await asyncio.gather(video_task, audio_task, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    video_task.cancel()
                    audio_task.cancel()
                    raise result

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
        """Асинхронное объединение видео и аудио с помощью FFmpeg с максимальной оптимизацией"""
        log_action("🔄 Объединение видео и аудио...")

        # Проверка существования файлов
        video_path = file_paths['video']
        audio_path = file_paths['audio']
        output_path = file_paths['output']
        if not os.path.exists(video_path) or not os.path.exists(audio_path):
            error_msg = f"Файл не найден: video={video_path}, audio={audio_path}"
            log_action(error_msg)
            raise FileNotFoundError(error_msg)

        # Команда для FFmpeg (оптимизированное объединение в MP4)
        merge_command = [
            'ffmpeg',
            '-i', video_path,  # Входной видео файл
            '-i', audio_path,  # Входной аудио файл
            '-c:v', 'copy',  # Копирование видео потока без перекодирования
            '-c:a', 'copy',  # Копирование аудио потока без перекодирования
            '-map', '0:v:0',  # Выбор видео потока из первого входного файла
            '-map', '1:a:0',  # Выбор аудио потока из второго входного файла
            '-f', 'mp4',  # Форсирование формата MP4
            '-y',  # Перезаписать выходной файл
            '-shortest',  # Остановить при окончании самого короткого потока
            output_path  # Выходной MP4 файл
        ]

        log_action(f"Выполняю команду: {' '.join(merge_command)}")

        # Асинхронный запуск FFmpeg
        merge_process = await asyncio.create_subprocess_exec(
            *merge_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await merge_process.communicate()

        # Проверка результата
        if merge_process.returncode == 0:
            log_action(f"✅ Готовый файл: {output_path}")
            return output_path
        else:
            error_message = stderr.decode().strip() if stderr else "Неизвестная ошибка FFmpeg"
            log_action(f"Ошибка FFmpeg (код {merge_process.returncode}): {error_message}")
            raise subprocess.CalledProcessError(merge_process.returncode, merge_command, output=stdout, stderr=stderr)

    async def _cleanup_temp_files(self, file_paths):
        for key in ['video', 'audio']:
            try:
                if os.path.exists(file_paths[key]):
                    os.remove(file_paths[key])
                    log_action(f"🧹 Удален временный файл: {file_paths[key]}")
            except Exception as e:
                log_action(f"⚠️ Ошибка при очистке: {e}")

    async def _get_direct_url(self, video_url, itags, fallback_itags=None):
        info = await get_video_info_with_cache(video_url)
        return await extract_url_from_info(info, itags, fallback_itags)

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

    async def _download_direct(self, url, filename, media_type, proxy_ports=None, num_parts=None):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Accept': '*/*',
                'Referer': 'https://www.youtube.com/'
            }

            timeout = aiohttp.ClientTimeout(total=600)

            redirect_count = 0
            max_redirects = 10
            current_url = url
            total = 0

            default_port = 9050
            ports = proxy_ports or [default_port]

            connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{ports[0]}')
            async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
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

            if media_type == 'audio':
                num_parts = num_parts or min(64, max(8, total // (1 * 1024 * 1024)))
            elif total < 100 * 1024 * 1024:
                num_parts = num_parts or min(96, max(16, total // (256 * 1024)))
            else:
                num_parts = num_parts or min(128, max(16, total // (5 * 1024 * 1024)))

            log_action(f"🔧 Использовано частей: {num_parts}")

            part_size = max(total // num_parts, 512 * 1024)

            ranges = []
            tail_boost_threshold = int(total * 0.85)
            i = 0
            while i < total:
                chunk = max(part_size // 2, 256 * 1024) if i >= tail_boost_threshold else part_size
                end = min(i + chunk - 1, total - 1)
                ranges.append((i, end))
                i += chunk

            prioritized_ranges = [(end - start, idx) for idx, (start, end) in enumerate(ranges)]
            heapq.heapify(prioritized_ranges)

            pbar = tqdm(total=total, unit='B', unit_scale=True, unit_divisor=1024, desc=media_type.upper())
            speed_map = {}
            start_time_all = time.time()

            sessions = {}
            for port in ports:
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                sessions[port] = aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector)

            completed = set()
            semaphore = asyncio.Semaphore(16)

            async def download_range(index):
                start, end = ranges[index]
                stream_id = f"{start}-{end}"
                part_file = f"{filename}.part{index}"
                port = ports[index % len(ports)]
                session = sessions[port]

                max_attempts = 5
                for attempt in range(1, max_attempts + 1):
                    try:
                        downloaded = 0
                        start_time = time.time()
                        range_headers = headers.copy()
                        range_headers['Range'] = f'bytes={start}-{end}'
                        async with semaphore:
                            async with session.get(current_url, headers=range_headers) as resp:
                                if resp.status in (403, 429, 409):
                                    raise aiohttp.ClientResponseError(resp.request_info, (), status=resp.status,
                                                                      message="Forbidden, Rate Limited or Conflict")
                                resp.raise_for_status()
                                async with aiofiles.open(part_file, 'wb') as f:
                                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                                        await f.write(chunk)
                                        chunk_len = len(chunk)
                                        downloaded += chunk_len
                                        pbar.update(chunk_len)

                        end_time = time.time()
                        duration = end_time - start_time
                        speed = downloaded / duration if duration > 0 else 0
                        speed_map[stream_id] = speed
                        completed.add(index)
                        return

                    except aiohttp.ClientResponseError as e:
                        if attempt < max_attempts:
                            log_action(
                                f"⚠️ {e.status} для диапазона {stream_id}, повтор через 10 сек (попытка {attempt}/{max_attempts})")
                            await asyncio.sleep(10)
                        else:
                            log_action(f"❌ Ошибка {e.status} при загрузке диапазона {stream_id}: {e}")
                    except Exception as e:
                        if attempt < max_attempts:
                            log_action(
                                f"⚠️ Ошибка при загрузке диапазона {stream_id}: {e} (попытка {attempt}/{max_attempts})")
                            await asyncio.sleep(5)
                        else:
                            log_action(f"❌ Ошибка загрузки диапазона {stream_id}: {e}")

            async def download_all():
                active = set()
                while prioritized_ranges or active:
                    while len(active) < 16 and prioritized_ranges:
                        _, index = heapq.heappop(prioritized_ranges)
                        task = asyncio.create_task(download_range(index))
                        active.add(task)
                    done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)

            await download_all()
            pbar.close()

            async with aiofiles.open(filename, 'wb') as outfile:
                for i in range(len(ranges)):
                    part_file = f"{filename}.part{i}"
                    async with aiofiles.open(part_file, 'rb') as pf:
                        while True:
                            chunk = await pf.read(1024 * 1024)
                            if not chunk:
                                break
                            await outfile.write(chunk)
                    os.remove(part_file)

            for session in sessions.values():
                await session.close()

            end_time_all = time.time()
            total_time = end_time_all - start_time_all
            avg_speed = total / total_time / (1024 * 1024)
            log_action(f"📊 Общее время: {total_time:.2f} сек | Средняя скорость: {avg_speed:.2f} MB/s")

            if speed_map:
                slowest = sorted(speed_map.items(), key=lambda x: x[1])[:5]
                for stream, spd in slowest:
                    log_action(f"🐼 Медленный поток {stream}: {spd / 1024:.2f} KB/s")

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
