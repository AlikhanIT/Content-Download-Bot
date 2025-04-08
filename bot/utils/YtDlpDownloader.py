import math
import os
import random
import uuid
import subprocess
from functools import cached_property
from fake_useragent import UserAgent
from bot.utils.tor_port_manager import ban_port, get_next_good_port
from bot.utils.video_info import get_video_info_with_cache, extract_url_from_info
import asyncio
from bot.utils.log import log_action
import contextlib

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
            log_action(f"❌ FFmpeg завершился с ошибкой {proc.returncode}")
            log_action(f"📤 FFmpeg stdout:\n{stdout.decode(errors='ignore')}")
            log_action(f"📥 FFmpeg stderr:\n{stderr.decode(errors='ignore')}")
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
        import aiofiles
        import aiohttp
        from aiohttp_socks import ProxyConnector
        import time
        import os
        from collections import defaultdict
        from tqdm import tqdm

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Accept': '*/*',
                'Referer': 'https://www.youtube.com/'
            }

            timeout = aiohttp.ClientTimeout(total=20)
            max_redirects = 10
            current_url = url
            total = 0

            default_port = 9050

            # Расчёт нужного количества портов
            TARGET_SPEED_MB_S = 10
            AVG_SPEED_PER_PORT_MB_S = 1.5
            required_ports = math.ceil(TARGET_SPEED_MB_S / AVG_SPEED_PER_PORT_MB_S)

            # Сборка уникальных портов через get_next_good_port
            selected_ports = set()
            while len(selected_ports) < required_ports:
                port = await get_next_good_port()
                selected_ports.add(port)

            ports = list(selected_ports)
            log_action(f"🌐 Выбрано {len(ports)} портов для загрузки: {ports}")
            port_403_counts = defaultdict(int)

            while True:
                for port in ports:
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
                                        current_url = location
                                        redirect_count += 1
                                        continue
                                    if r.status in (403, 429):
                                        port_403_counts[port] += 1
                                        if port_403_counts[port] >= 5:
                                            await ban_port(port)
                                        raise aiohttp.ClientResponseError(r.request_info, (), status=r.status,
                                                                          message="Forbidden or Rate Limited")
                                    r.raise_for_status()
                                    total = int(r.headers.get('Content-Length', 0))
                                    if total == 0:
                                        raise ValueError("Не удалось определить размер файла")
                                    break
                        if total > 0:
                            break
                    except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                        await ban_port(port)
                        continue
                    except Exception:
                        continue
                else:
                    await asyncio.sleep(1)
                    continue
                break

            if not num_parts:
                target_chunk_size = 10 * 1024 * 1024  # 10 MB
                min_parts = 4  # Было 1–2 на мелких файлах, станет 4
                max_parts = 512

                num_parts = total // target_chunk_size
                num_parts = max(min_parts, min(max_parts, num_parts))
                if num_parts == 0:
                    num_parts = min_parts

            part_size = total // num_parts
            min_chunk_size = 2 * 1024 * 1024
            ranges = []
            i = 0
            while i < num_parts:
                start = i * part_size
                end = min((i + 1) * part_size - 1, total - 1)
                if total - end < min_chunk_size * 3 and i < num_parts - 1:
                    end = total - 1
                    ranges.append((start, end))
                    break
                ranges.append((start, end))
                i += 1

            remaining = set(range(len(ranges)))
            pbar = tqdm(total=total, unit='B', unit_scale=True, unit_divisor=1024, desc=media_type.upper())
            speed_map = {}
            start_time_all = time.time()

            sessions = {}
            try:
                for port in ports:
                    connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                    sessions[port] = aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector)

                semaphore = asyncio.Semaphore(min(num_parts, 64))

                async def download_range(index):
                    start, end = ranges[index]
                    stream_id = f"{start}-{end}"
                    part_file = f"{filename}.part{index}"
                    attempt = 0

                    while True:
                        attempt += 1
                        port = random.choice(ports)
                        if not port:
                            log_action(f"❌ Нет доступных прокси-портов для {stream_id}, ожидание...")
                            await asyncio.sleep(3)
                            continue

                        try:
                            # Создание сессии при первом использовании
                            if port not in sessions or sessions[port].closed:
                                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                                sessions[port] = aiohttp.ClientSession(headers=headers, timeout=timeout,
                                                                       connector=connector)

                            session = sessions[port]
                            downloaded = 0
                            start_time = time.time()
                            range_headers = headers.copy()
                            range_headers['Range'] = f'bytes={start}-{end}'

                            async with semaphore:
                                async with session.get(current_url, headers=range_headers) as resp:
                                    if resp.status in (403, 429, 409):
                                        log_action(f"🚫 Статус {resp.status} для {stream_id} через порт {port} — баним")
                                        await ban_port(port)
                                        continue

                                    resp.raise_for_status()
                                    async with aiofiles.open(part_file, 'wb') as f:
                                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                                            await f.write(chunk)
                                            downloaded += len(chunk)
                                            pbar.update(len(chunk))

                            # Проверка на отсутствие файла или нулевой размер
                            if not os.path.exists(part_file) or os.path.getsize(part_file) == 0:
                                log_action(f"⚠️ Файл части {part_file} не создан или пустой — баним порт {port}")
                                await ban_port(port)
                                await asyncio.sleep(2)
                                continue

                            # Вычисление скорости и бан медленного порта
                            duration = time.time() - start_time
                            speed = downloaded / duration if duration > 0 else 0
                            speed_map[stream_id] = speed

                            if speed < 100 * 1024:  # < 100 KB/s
                                log_action(f"🐌 Низкая скорость: {speed / 1024:.2f} KB/s — баним порт {port}")
                                await ban_port(port)

                            remaining.discard(index)
                            return

                        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                            log_action(f"⚠️ Ошибка соединения на порту {port} для {stream_id} — баним")
                            await ban_port(port)
                            await asyncio.sleep(2)
                            continue

                        except aiohttp.ClientResponseError as e:
                            log_action(f"⚠️ HTTP {e.status} для {stream_id} через порт {port} — баним")
                            await ban_port(port)
                            await asyncio.sleep(1)
                            continue

                        except Exception as e:
                            log_action(f"❌ Неизвестная ошибка {e} при загрузке {stream_id} через порт {port} — баним")
                            await ban_port(port)
                            await asyncio.sleep(3)
                            continue

                await asyncio.gather(*(download_range(i) for i in range(len(ranges))))

                if remaining:
                    raise Exception(f"Не удалось скачать все части: {sorted(remaining)}")

            finally:
                for port, session in sessions.items():
                    with contextlib.suppress(Exception):
                        if not session.closed:
                            await session.close()

            pbar.close()

            try:
                async with aiofiles.open(filename, 'wb') as outfile:
                    for i in range(len(ranges)):
                        part_file = f"{filename}.part{i}"
                        if not os.path.exists(part_file) or os.path.getsize(part_file) == 0:
                            raise FileNotFoundError(f"Файл части не найден или пустой: {part_file}")
                        async with aiofiles.open(part_file, 'rb') as pf:
                            while True:
                                chunk = await pf.read(1024 * 1024)
                                if not chunk:
                                    break
                                await outfile.write(chunk)
                        os.remove(part_file)

                if not os.path.exists(filename) or os.path.getsize(filename) == 0:
                    raise Exception(f"Итоговый файл {filename} не создан или пустой")

            except Exception as e:
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

