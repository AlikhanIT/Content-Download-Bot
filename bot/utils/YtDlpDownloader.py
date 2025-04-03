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

import asyncio
import time
import stem
import stem.control

from bot.utils.log import log_action


class TorInstanceManager:
    def __init__(self, base_control_port=9051, count=40):
        self.control_ports = [base_control_port + i * 2 for i in range(count)]
        self.locks = {port: asyncio.Lock() for port in self.control_ports}
        self.last_changed = {port: 0 for port in self.control_ports}

    async def renew_identity(self, index):
        port = self.control_ports[index]
        now = time.time()

        # Минимум 10 сек между сменами для одного инстанса
        if now - self.last_changed[port] < 10:
            return

        async with self.locks[port]:
            try:
                with stem.control.Controller.from_port(port=port) as controller:
                    controller.authenticate()  # если есть пароль: controller.authenticate(password='xxx')
                    controller.signal(stem.Signal.NEWNYM)
                    self.last_changed[port] = time.time()
                    log_action(f"♻️ IP обновлён через контрол порт {port}")
            except Exception as e:
                log_action(f"❌ Ошибка при NEWNYM для порта {port}: {e}")


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
        self.tor_manager = TorInstanceManager()

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
            proxy_ports = proxy_ports or [9050]
            banned_ports = {}
            port_403_counts = defaultdict(int)
            fast_ports = set()

            # HEAD-запрос для получения размера файла
            total = 0
            for port in proxy_ports:
                if banned_ports.get(port, 0) > time.time():
                    continue
                try:
                    connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
                        async with session.head(url, allow_redirects=True) as r:
                            r.raise_for_status()
                            total = int(r.headers.get('Content-Length', 0))
                            break
                except Exception as e:
                    log_action(f"HEAD-фейл {port}: {e}")
                    continue

            if total == 0:
                raise ValueError("Файл пуст или HEAD-запрос не удался")

            # Адаптивный размер чанков
            if total < 10 * 1024 * 1024:
                part_size = 1 * 1024 * 1024
            elif total < 200 * 1024 * 1024:
                part_size = 2 * 1024 * 1024
            else:
                part_size = 4 * 1024 * 1024

            ranges = [(i * part_size, min((i + 1) * part_size - 1, total - 1)) for i in
                      range((total + part_size - 1) // part_size)]
            chunk_queue = asyncio.Queue()
            for idx, rng in enumerate(ranges):
                await chunk_queue.put((idx, rng))

            part_file_map = {}
            pbar = tqdm(total=total, unit='B', unit_scale=True, unit_divisor=1024, desc=media_type.upper())
            sessions = {}

            semaphore = asyncio.Semaphore(24)
            speed_log = defaultdict(list)

            for port in proxy_ports:
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                sessions[port] = aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector)

            async def handle_chunk():
                while not chunk_queue.empty():
                    try:
                        idx, (start, end) = await chunk_queue.get()
                        assigned = False
                        tried_ports = set()

                        for _ in range(len(proxy_ports)):
                            port = min(
                                (p for p in proxy_ports if
                                 p not in tried_ports and banned_ports.get(p, 0) < time.time()),
                                key=lambda p: -1 if p in fast_ports else 0,
                                default=None
                            )

                            if port is None:
                                await asyncio.sleep(1)
                                continue

                            tried_ports.add(port)
                            session = sessions[port]
                            part_file = f"{filename}.part{idx}"
                            headers['Range'] = f'bytes={start}-{end}'

                            try:
                                start_time = time.time()
                                downloaded = 0
                                async with semaphore:
                                    async with session.get(url, headers=headers) as r:
                                        r.raise_for_status()
                                        async with aiofiles.open(part_file, 'wb') as f:
                                            async for chunk in r.content.iter_chunked(1024 * 64):
                                                await f.write(chunk)
                                                downloaded += len(chunk)
                                                pbar.update(len(chunk))

                                                # Early speed check
                                                elapsed = time.time() - start_time
                                                if elapsed >= 1.5 and downloaded / elapsed < 100 * 1024:
                                                    log_action(
                                                        f"🐢 Порт {port} слишком медленный — IP смена, retry chunk")
                                                    await self.tor_manager.renew_identity(proxy_ports.index(port))
                                                    await chunk_queue.put((idx, (start, end)))
                                                    raise Exception("Early slow port")

                                duration = time.time() - start_time
                                speed = downloaded / duration
                                speed_log[port].append(speed)

                                if speed > 4 * 1024 * 1024:
                                    fast_ports.add(port)

                                part_file_map[idx] = part_file
                                assigned = True
                                break

                            except Exception as e:
                                log_action(f"❌ Ошибка на порту {port} (чанк {idx}): {e}")
                                await self.tor_manager.renew_identity(proxy_ports.index(port))

                        if not assigned:
                            await chunk_queue.put((idx, (start, end)))

                    except Exception as e:
                        log_action(f"🔥 Чанк {idx} критически упал: {e}")

            await asyncio.gather(*[handle_chunk() for _ in range(min(len(ranges), 24))])

            for session in sessions.values():
                await session.close()

            pbar.close()

            async with aiofiles.open(filename, 'wb') as out:
                for i in range(len(ranges)):
                    pf = part_file_map.get(i)
                    if not pf or not os.path.exists(pf):
                        raise FileNotFoundError(f"Файл части не найден: {pf}")
                    async with aiofiles.open(pf, 'rb') as src:
                        while True:
                            chunk = await src.read(1024 * 1024)
                            if not chunk:
                                break
                            await out.write(chunk)
                    os.remove(pf)

            log_action(f"✅ Скачано: {filename}")
            for port, speeds in speed_log.items():
                avg = sum(speeds) / len(speeds)
                log_action(f"📈 Порт {port} — средняя скорость: {avg / 1024:.2f} KB/s")

        except Exception as e:
            log_action(f"❌ Ошибка при загрузке: {e}")
            raise
