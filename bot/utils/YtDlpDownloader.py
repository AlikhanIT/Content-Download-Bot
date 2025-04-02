import asyncio
import json
import os
import time
import uuid
import subprocess

import aiofiles
import aiohttp
from aiohttp_socks import ProxyConnector
from tqdm import tqdm
from collections import defaultdict
from functools import cached_property
from fake_useragent import UserAgent

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
            TOR_INSTANCES = 40
            proxy_ports = [9050 + i * 2 for i in range(TOR_INSTANCES)]
            info = await get_video_info_with_cache(url)

            if download_type == "audio":
                direct_audio_url = await extract_url_from_info(info, ["249", "250", "251", "140"])
                await self._download_direct(direct_audio_url, file_paths['audio'], media_type='audio', proxy_ports=proxy_ports)
                return file_paths['audio']

            video_itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
            video_url_task = asyncio.create_task(extract_url_from_info(info, [video_itag]))
            audio_url_task = asyncio.create_task(extract_url_from_info(info, ["249", "250", "251", "140"]))
            direct_video_url, direct_audio_url = await asyncio.gather(video_url_task, audio_url_task)

            video_task = asyncio.create_task(self._download_direct(direct_video_url, file_paths['video'], media_type='video', proxy_ports=proxy_ports))
            audio_task = asyncio.create_task(self._download_direct(direct_audio_url, file_paths['audio'], media_type='audio', proxy_ports=proxy_ports))
            await asyncio.gather(video_task, audio_task)

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
        video_path = file_paths['video']
        audio_path = file_paths['audio']
        output_path = file_paths['output']

        merge_command = [
            'ffmpeg', '-i', video_path, '-i', audio_path,
            '-c:v', 'copy', '-c:a', 'copy',
            '-map', '0:v:0', '-map', '1:a:0',
            '-f', 'mp4', '-y', '-shortest', output_path
        ]

        log_action(f"Выполняю команду: {' '.join(merge_command)}")
        proc = await asyncio.create_subprocess_exec(*merge_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            log_action(f"✅ Готовый файл: {output_path}")
            return output_path
        else:
            raise subprocess.CalledProcessError(proc.returncode, merge_command, stdout, stderr)

    async def _cleanup_temp_files(self, file_paths):
        for key in ['video', 'audio']:
            try:
                if os.path.exists(file_paths[key]):
                    os.remove(file_paths[key])
                    log_action(f"🧹 Удален временный файл: {file_paths[key]}")
            except Exception as e:
                log_action(f"⚠️ Ошибка при очистке: {e}")

    async def _download_direct(self, url, filename, media_type, proxy_ports=None, num_parts=None):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Accept': '*/*',
                'Referer': 'https://www.youtube.com/'
            }

            timeout = aiohttp.ClientTimeout(total=20)
            redirect_count = 0
            max_redirects = 10
            current_url = url
            total = 0

            default_port = 9050
            ports = proxy_ports or [default_port]
            banned_ports = {}
            port_403_counts = defaultdict(int)

            # Повторять пока не получим Content-Length или не исчерпаем порты
            while True:
                for port in ports:
                    if banned_ports.get(port, 0) > time.time():
                        continue
                    try:
                        connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                        async with aiohttp.ClientSession(headers=headers, timeout=timeout,
                                                         connector=connector) as session:
                            redirect_count = 0
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
                                    if r.status in (403, 429):
                                        port_403_counts[port] += 1
                                        if port_403_counts[port] >= 5:
                                            banned_ports[port] = time.time() + 600
                                            log_action(
                                                f"🚫 Порт {port} забанен на 10 мин после {port_403_counts[port]} ошибок 403")
                                        raise aiohttp.ClientResponseError(r.request_info, (), status=r.status,
                                                                          message="Forbidden or Rate Limited")
                                    r.raise_for_status()
                                    total = int(r.headers.get('Content-Length', 0))
                                    if total == 0:
                                        raise ValueError("Не удалось определить размер файла")
                                    break
                        if total > 0:
                            break  # успешный выход из внешнего while
                    except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                        banned_ports[port] = time.time() + 300
                        log_action(f"⚠️ Порт {port} получил таймаут — временно исключён")
                        continue
                    except Exception as e:
                        log_action(f"⚠️ Ошибка HEAD-запроса с портом {port}: {e}")
                        continue
                else:
                    await asyncio.sleep(1)
                    continue
                break  # успешный HEAD-запрос — выход из while

            total_mb = total / (1024 * 1024)
            log_action(f"⬇️ Начало загрузки {media_type.upper()}: {total_mb:.2f} MB — {filename}")

            num_parts = num_parts or (min(256, max(128, total // (256 * 1024))) if media_type == 'audio' else min(512,
                                                                                                                  max(192,
                                                                                                                      total // (
                                                                                                                                  512 * 1024))))
            log_action(f"🔧 Использовано частей: {num_parts}")

            part_size = total // num_parts
            ranges = [(i * part_size, min((i + 1) * part_size - 1, total - 1)) for i in range(num_parts)]
            remaining = set(range(len(ranges)))

            pbar = tqdm(total=total, unit='B', unit_scale=True, unit_divisor=1024, desc=media_type.upper())
            speed_map = {}
            start_time_all = time.time()

            sessions = {}
            try:
                for port in ports:
                    connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                    sessions[port] = aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector)

                semaphore = asyncio.Semaphore(24)

                async def download_range(index):
                    try:
                        start, end = ranges[index]
                        stream_id = f"{start}-{end}"
                        part_file = f"{filename}.part{index}"
                        max_attempts = 20

                        for attempt in range(1, max_attempts + 1):
                            available_ports = [p for p in ports if banned_ports.get(p, 0) < time.time()]
                            if not available_ports:
                                log_action("❌ Нет доступных прокси-портов")
                                raise Exception("Нет доступных прокси-портов")
                            port = available_ports[(index + attempt) % len(available_ports)]
                            session = sessions.get(port)
                            if not session or session.closed:
                                log_action(f"⚠️ Сессия для порта {port} недоступна, попытка {attempt}/{max_attempts}")
                                await asyncio.sleep(1)
                                continue

                            try:
                                downloaded = 0
                                start_time = time.time()
                                range_headers = headers.copy()
                                range_headers['Range'] = f'bytes={start}-{end}'

                                async with semaphore:
                                    async with session.get(current_url, headers=range_headers) as resp:
                                        if resp.status in (403, 429, 409):
                                            port_403_counts[port] += 1
                                            if port_403_counts[port] >= 5:
                                                banned_ports[port] = time.time() + 600
                                                log_action(
                                                    f"🚫 Порт {port} забанен на 10 мин после {port_403_counts[port]} ошибок 403")
                                            raise aiohttp.ClientResponseError(resp.request_info, (), status=resp.status,
                                                                              message="Forbidden, Rate Limited or Conflict")
                                        resp.raise_for_status()
                                        async with aiofiles.open(part_file, 'wb') as f:
                                            downloaded = 0
                                            chunk_start_time = time.time()
                                            chunk_timer = time.time()
                                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                                await f.write(chunk)
                                                chunk_len = len(chunk)
                                                downloaded += chunk_len
                                                pbar.update(chunk_len)

                                                elapsed = time.time() - chunk_start_time
                                                if downloaded >= 10 * 1024 * 1024:
                                                    duration10 = time.time() - chunk_timer
                                                    log_action(
                                                        f"📈 Поток {stream_id}, порт {port}, загружено 10MB за {duration10:.2f} сек")
                                                    chunk_timer = time.time()
                                                    downloaded = 0

                                                if elapsed >= 5:
                                                    speed_now = downloaded / elapsed
                                                    if speed_now < 20 * 1024:
                                                        log_action(
                                                            f"🐵 Слишком медленно ({speed_now / 1024:.2f} KB/s) для диапазона {stream_id}, порт {port} — пробую заново")
                                                        raise Exception(
                                                            "Медленная загрузка, перезапуск с другим портом")

                                duration = time.time() - start_time
                                speed = downloaded / duration if duration > 0 else 0
                                speed_map[stream_id] = speed
                                remaining.discard(index)
                                return

                            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                                banned_ports[port] = time.time() + 300
                                log_action(f"⚠️ Порт {port} получил таймаут — временно исключён")
                                continue
                            except aiohttp.ClientResponseError as e:
                                if e.status in (403, 429, 409):
                                    log_action(
                                        f"⚠️ Ошибка {e.status}, message='{e.message}' для {stream_id}, порт {port}")
                                    continue
                                else:
                                    log_action(f"❌ Необрабатываемая ошибка {e.status} для {stream_id}: {e}")
                                    raise
                            except Exception as e:
                                log_action(
                                    f"❌ Ошибка {e} для {stream_id}, попытка {attempt}/{max_attempts}, порт {port}")
                                await asyncio.sleep(3)
                                continue

                        log_action(f"❌ Провал загрузки диапазона {stream_id} после {max_attempts} попыток")
                        raise ValueError(f"Не удалось загрузить {stream_id} после {max_attempts} попыток")

                    except Exception as e:
                        log_action(f"❌ Непредвиденная ошибка в задаче {index}: {e}")
                        raise

                await asyncio.gather(*(download_range(i) for i in range(len(ranges))))

                if remaining:
                    raise Exception(f"Не удалось скачать все части: {sorted(remaining)}")

            finally:
                for session in sessions.values():
                    await session.close()

            pbar.close()

            try:
                async with aiofiles.open(filename, 'wb') as outfile:
                    for i in range(len(ranges)):
                        part_file = f"{filename}.part{i}"
                        if not os.path.exists(part_file):
                            log_action(f"⚠️ Файл части не найден: {part_file}")
                            raise FileNotFoundError(f"Файл части не найден: {part_file}")
                        async with aiofiles.open(part_file, 'rb') as pf:
                            while True:
                                chunk = await pf.read(1024 * 1024)
                                if not chunk:
                                    break
                                await outfile.write(chunk)
                        os.remove(part_file)
            except Exception as e:
                log_action(f"❌ Ошибка при объединении файлов: {e}")
                raise

            total_time = time.time() - start_time_all
            avg_speed = total / total_time / (1024 * 1024)
            log_action(f"📊 Общее время: {total_time:.2f} сек | Средняя скорость: {avg_speed:.2f} MB/s")

            if speed_map:
                slowest = sorted(speed_map.items(), key=lambda x: x[1])[:5]
                for stream, spd in slowest:
                    log_action(f"🐵 Медленный поток {stream}: {spd / 1024:.2f} KB/s")


            log_action(f"✅ Скачивание завершено: {filename}")

        except Exception as e:
            log_action(f"❌ Ошибка при скачивании {filename}: {e}")