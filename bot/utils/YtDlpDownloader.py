import os
import uuid
import subprocess
import aiohttp
from aiohttp_socks import ProxyConnector
from functools import cached_property
from fake_useragent import UserAgent
from bot.utils.video_info import get_video_info_with_cache, extract_url_from_info
import asyncio
import time
import stem
import stem.control
from bot.utils.log import log_action


class TorPortManager:
    def __init__(self, count=40):
        self.lock = asyncio.Lock()
        self.ports = [9050 + i * 2 for i in range(count)]
        self.control_ports = [9051 + i * 2 for i in range(count)]
        self.port_status = {
            port: {
                "failures": 0,
                "last_ip_change": 0,
                "speed": 0,
                "recovering": False
            } for port in self.ports
        }
        self.speed_history = []  # (timestamp, speed)
        self.speed_history_max = 300  # храним до 300 записей (примерно последние 5 минут)

    async def get_best_port(self, exclude_ports=None):
        exclude_ports = exclude_ports or set()
        now = time.time()
        available = [
            p for p in self.ports
            if p not in exclude_ports and not self.port_status[p]["recovering"]
        ]
        ranked = sorted(
            available,
            key=lambda p: -self.port_status[p]["speed"] if now - self.port_status[p]["last_ip_change"] < 60 else -1
        )
        if ranked:
            return ranked[0]
        raise Exception("❌ Нет доступных портов")

    async def report_speed(self, port, speed):
        self.port_status[port]["speed"] = speed
        self.port_status[port]["last_ip_change"] = time.time()
        self.speed_history.append((time.time(), speed))
        if len(self.speed_history) > self.speed_history_max:
            self.speed_history = self.speed_history[-self.speed_history_max:]

    async def get_average_speed(self):
        now = time.time()
        recent = [s for t, s in self.speed_history if now - t <= 60]
        if not recent:
            return 100 * 1024  # fallback = 100 KB/s
        return sum(recent) / len(recent)

    async def report_failure(self, port):
        if self.port_status[port]["recovering"]:
            return
        self.port_status[port]["recovering"] = True
        asyncio.create_task(self.recover_port(port))

    async def recover_port(self, port):
        index = self.ports.index(port)
        control_port = self.control_ports[index]
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': '*/*',
        }
        test_url = "https://www.youtube.com"

        while True:
            try:
                # смена IP
                async with self.lock:
                    try:
                        with stem.control.Controller.from_port(port=control_port) as controller:
                            controller.authenticate()
                            controller.signal(stem.Signal.NEWNYM)
                            self.port_status[port]["last_ip_change"] = time.time()
                            log_action(f"♻️ Перезапрос IP для порта {port}")
                    except Exception as e:
                        log_action(f"⚠️ Ошибка NEWNYM на порту {port}: {e}")

                # HEAD-запрос
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
                    async with session.head(test_url, allow_redirects=False) as r:
                        if r.status == 200:
                            self.port_status[port]["recovering"] = False
                            self.port_status[port]["failures"] = 0
                            self.port_status[port]["speed"] = 100000  # базовая скорость
                            log_action(f"✅ Порт {port} восстановлен")
                            return
                        else:
                            log_action(f"🚫 Порт {port} отдаёт {r.status}")
            except Exception as e:
                log_action(f"🔄 Порт {port} ещё не восстановлен: {e}")

            await asyncio.sleep(5)

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
        self.tor_manager = TorPortManager()

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
        import aiohttp
        import aiofiles
        from aiohttp_socks import ProxyConnector
        import time
        import os
        from tqdm import tqdm

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': '*/*',
            'Referer': 'https://www.youtube.com/'
        }

        timeout = aiohttp.ClientTimeout(total=20)
        ports = proxy_ports or [9050 + i * 2 for i in range(40)]

        # Получаем размер
        total = 0
        for port in ports:
            try:
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
                    async with session.head(url, allow_redirects=True) as r:
                        r.raise_for_status()
                        total = int(r.headers.get('Content-Length', 0))
                        if total > 0:
                            break
            except Exception as e:
                log_action(f"⚠️ HEAD-запрос не удался на порту {port}: {e}")
                continue

        if total == 0:
            raise Exception("❌ Не удалось определить размер файла")

        log_action(f"⬇️ Скачивание {media_type.upper()} | Размер: {total / 1024 / 1024:.2f} MB")

        num_parts = num_parts or (min(256, max(128, total // (256 * 1024))) if media_type == 'audio' else min(512,
                                                                                                              max(192,
                                                                                                                  total // (
                                                                                                                              512 * 1024))))
        part_size = total // num_parts
        ranges = [(i * part_size, min((i + 1) * part_size - 1, total - 1)) for i in range(num_parts)]

        pbar = tqdm(total=total, unit='B', unit_scale=True, desc=media_type.upper())
        semaphore = asyncio.Semaphore(24)
        start_time_all = time.time()

        async def download_range(index):
            start, end = ranges[index]
            stream_id = f"{start}-{end}"
            part_file = f"{filename}.part{index}"
            max_attempts = 20
            attempt = 0
            used_ports = set()

            while attempt < max_attempts:
                attempt += 1
                try:
                    port = await self.tor_manager.get_best_port(exclude_ports=used_ports)
                    connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
                        async with semaphore:
                            async with session.get(url, headers={'Range': f'bytes={start}-{end}'}) as resp:
                                if resp.status in (403, 429):
                                    await self.tor_manager.report_failure(port)
                                    used_ports.add(port)
                                    continue
                                resp.raise_for_status()

                                async with aiofiles.open(part_file, 'wb') as f:
                                    start_time = time.time()
                                    downloaded = 0

                                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                                        await f.write(chunk)
                                        downloaded += len(chunk)
                                        pbar.update(len(chunk))

                                        elapsed = time.time() - start_time

                                        if elapsed >= 10:  # проверяем скорость каждые 10 сек
                                            speed_now = downloaded / elapsed
                                            avg_speed = await self.tor_manager.get_average_speed()

                                            if speed_now < avg_speed * 0.5:
                                                log_action(
                                                    f"🐢 Поток {stream_id} слишком медленный ({speed_now / 1024:.1f} KB/s < {avg_speed / 1024:.1f} KB/s), смена порта")
                                                await self.tor_manager.report_failure(port)
                                                used_ports.add(port)
                                                raise Exception("Медленно, перезапуск")

                                            # сбросим счётчики
                                            downloaded = 0
                                            start_time = time.time()

                                duration = time.time() - start_time
                                await self.tor_manager.report_speed(port, downloaded / duration)
                                return
                except Exception as e:
                    log_action(f"❌ Попытка {attempt}/{max_attempts} не удалась для {stream_id}: {e}")
                    await asyncio.sleep(1)

            raise Exception(f"❌ Не удалось скачать диапазон {stream_id} после {max_attempts} попыток")

        await asyncio.gather(*(download_range(i) for i in range(len(ranges))))

        pbar.close()

        # Склеиваем части
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

        total_time = time.time() - start_time_all
        avg_speed = total / total_time / (1024 * 1024)
        log_action(f"📊 Время: {total_time:.2f} сек | Ср. скорость: {avg_speed:.2f} MB/s")
        log_action(f"✅ Готово: {filename}")
