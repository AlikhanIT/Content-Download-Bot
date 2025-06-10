#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import uuid
import subprocess
import asyncio
import time
from functools import cached_property
from contextlib import asynccontextmanager

import aiofiles
import aiohttp
from aiohttp_socks import ProxyConnector
from fake_useragent import UserAgent
from tqdm import tqdm

from bot.utils.tor_port_manager import ban_port, get_next_good_port
from bot.utils.video_info import get_video_info_with_cache, extract_url_from_info
from bot.utils.log import log_action


class PortPool:
    def __init__(self, ports):
        self._ports = ports[:]
        self._sem = asyncio.Semaphore(len(self._ports))
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self):
        await self._sem.acquire()
        async with self._lock:
            port = self._ports.pop(0)
        try:
            yield port
        finally:
            async with self._lock:
                self._ports.append(port)
            self._sem.release()


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

    async def download(self, url, download_type="video", quality="480", progress_msg=None):
        start_time = time.time()
        await self.start_workers()
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        await self.queue.put((url, download_type, quality, future, progress_msg))
        result = await future
        try:
            size = os.path.getsize(result)
            duration = time.time() - start_time
            avg = size / duration if duration > 0 else 0
            log_action(f"📊 Finished: {size/1024/1024:.2f} MB in {duration:.2f}s ({avg/1024/1024:.2f} MB/s)")
        except Exception:
            pass
        return result

    async def _worker(self):
        while True:
            url, download_type, quality, future, progress_msg = await self.queue.get()
            try:
                res = await self._process_download(url, download_type, quality, progress_msg)
                future.set_result(res)
            except Exception as e:
                future.set_exception(e)
            finally:
                self.queue.task_done()

    async def _process_download(self, url, download_type, quality, progress_msg):
        start_proc = time.time()
        file_paths = await self._prepare_file_paths(download_type)
        result = None
        try:
            ports = [9050 + i * 2 for i in range(40)]
            info = await get_video_info_with_cache(url)
            if download_type == "audio":
                audio_url = await extract_url_from_info(info, [self.DEFAULT_AUDIO_ITAG])
                result = await self._download_direct(audio_url, file_paths['audio'], 'audio', ports, progress_msg)
            else:
                itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
                video_url, audio_url = await asyncio.gather(
                    extract_url_from_info(info, [itag]),
                    extract_url_from_info(info, [self.DEFAULT_AUDIO_ITAG])
                )
                await asyncio.gather(
                    self._download_direct(video_url, file_paths['video'], 'video', ports, progress_msg),
                    self._download_direct(audio_url, file_paths['audio'], 'audio', ports, progress_msg)
                )
                result = await self._merge_files(file_paths)
            return result
        finally:
            try:
                size = os.path.getsize(result) if result and os.path.exists(result) else 0
                duration = time.time() - start_proc
                avg = size / duration if duration > 0 else 0
                log_action(f"📈 Process: {download_type.upper()} {size/1024/1024:.2f} MB in {duration:.2f}s ({avg/1024/1024:.2f} MB/s)")
            except Exception:
                pass
            if download_type != 'audio':
                await self._cleanup_temp_files(file_paths)

    async def _prepare_file_paths(self, download_type):
        rnd = uuid.uuid4()
        base = {'output': os.path.join(self.DOWNLOAD_DIR, f"{rnd}.mp4")}
        if download_type == 'audio':
            base['audio'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}.m4a")
        else:
            base['video'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_video.mp4")
            base['audio'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_audio.m4a")
        return base

    async def _merge_files(self, file_paths):
        log_action("🔄 Merging видео и аудио...")
        cmd = [
            'ffmpeg', '-i', file_paths['video'], '-i', file_paths['audio'],
            '-c:v', 'copy', '-c:a', 'copy', '-map', '0:v:0', '-map', '1:a:0',
            '-y', file_paths['output']
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            log_action(f"❌ FFmpeg error {proc.returncode}: {err.decode()}")
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        log_action(f"✅ Output: {file_paths['output']}")
        return file_paths['output']

    async def _cleanup_temp_files(self, file_paths):
        for key in ['video', 'audio']:
            fp = file_paths.get(key)
            if fp and os.path.exists(fp):
                os.remove(fp)
                log_action(f"🧹 Removed temp: {fp}")

    async def _download_direct(self, url, filename, media, ports, progress_msg):
        # 1) Получаем общий размер файла
        headers = {'User-Agent': self.user_agent.chrome}
        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as sess:
            async with sess.head(url, allow_redirects=True) as r:
                r.raise_for_status()
                total = int(r.headers.get('Content-Length', 0))
        if total <= 0:
            raise Exception("Не удалось получить размер файла")

        # 2) Разбиваем на блоки
        block_size = max(total // (len(ports) * 4), 2 * 1024 * 1024)
        blocks = [
            (i * block_size, min((i + 1) * block_size - 1, total - 1))
            for i in range((total + block_size - 1) // block_size)
        ]

        # 3) Прогресс-бар
        pbar = tqdm(total=total, unit='B', unit_scale=True, desc=media.upper(), dynamic_ncols=True)
        t0 = time.time()

        # 4) Инициализируем пул портов и структуру для скоростей
        pool = PortPool(ports)
        speed_map = {}
        speeds_lock = asyncio.Lock()

        async def download_block(start, end):
            part_path = f"{filename}.part{start}-{end}"
            attempts = 0
            while True:
                attempts += 1
                async with pool.acquire() as port:
                    conn = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                    try:
                        block_t0 = time.time()
                        async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=conn) as sess:
                            async with sess.get(url, headers={'Range': f'bytes={start}-{end}'}) as resp:
                                resp.raise_for_status()
                                data = await resp.content.readexactly(end - start + 1)

                        elapsed = time.time() - block_t0
                        speed = len(data) / elapsed if elapsed > 0 else 0
                        async with speeds_lock:
                            speed_map[port] = speed
                            avg_speed = sum(speed_map.values()) / len(speed_map)

                        if speed < avg_speed * 0.7:
                            await ban_port(port)
                            continue

                        async with aiofiles.open(part_path, 'wb') as f:
                            await f.write(data)
                        pbar.update(len(data))
                        return
                    except Exception:
                        await ban_port(port)
                        if attempts >= YtDlpDownloader.MAX_RETRIES:
                            raise

        # 5) Запускаем задачи на загрузку всех блоков
        tasks = [asyncio.create_task(download_block(s, e)) for s, e in blocks]
        await asyncio.gather(*tasks)
        pbar.close()

        # 6) Склеиваем части в итоговый файл
        async with aiofiles.open(filename, 'wb') as out:
            for start, end in blocks:
                part_path = f"{filename}.part{start}-{end}"
                async with aiofiles.open(part_path, 'rb') as pf:
                    chunk = await pf.read()
                    await out.write(chunk)
                os.remove(part_path)

        # 7) Финальный лог
        duration = time.time() - t0
        avg = total / duration if duration > 0 else 0
        log_action(f"✅ {media.upper()} {total/1024/1024:.2f}MB за {duration:.2f}s ({avg/1024/1024:.2f} MB/s)")

        return filename


def safe_log(msg):
    tqdm.write(msg)
    log_action(msg)
