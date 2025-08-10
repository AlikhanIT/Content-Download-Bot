#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import platform
import stat
import uuid
import subprocess
import asyncio
import time
import shutil
from functools import cached_property

import aiofiles  # если не используется — можно убрать
from fake_useragent import UserAgent
from tqdm import tqdm

from bot.utils.video_info import get_video_info_with_cache, extract_url_from_info
from bot.utils.log import log_action


class PortPool:
    def __init__(self, ports):
        self._ports = ports[:]
        self._current_index = 0
        self._lock = asyncio.Lock()

    async def get_next_port(self):
        async with self._lock:
            port = self._ports[self._current_index]
            self._current_index = (self._current_index + 1) % len(self._ports)
            return port


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

        # ── ЕДИНЫЙ Tor порт(ы) из ENV, по умолчанию 9050 ─────────────────────
        ports_env = os.getenv("TOR_SOCKS_PORTS", "9050")
        ports = [int(p.strip()) for p in ports_env.split(",") if p.strip()]
        if not ports:
            ports = [9050]
        self.port_pool = PortPool(ports)

    def _ensure_download_dir(self):
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        log_action(f"📂 Папка для загрузки: {self.DOWNLOAD_DIR}")

    @cached_property
    def user_agent(self):
        # если fake_useragent не может обновиться, .random может падать — завернём в try в месте использования
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
            log_action(f"📊 Finished: {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
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
            info = await get_video_info_with_cache(url)

            if download_type == "audio":
                audio_url = await extract_url_from_info(info, [self.DEFAULT_AUDIO_ITAG])
                result = await self._download_with_tordl(audio_url, file_paths['audio'], 'audio', progress_msg)
            else:
                itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
                video_url, audio_url = await asyncio.gather(
                    extract_url_from_info(info, [itag]),
                    extract_url_from_info(info, [self.DEFAULT_AUDIO_ITAG])
                )
                await asyncio.gather(
                    self._download_with_tordl(video_url, file_paths['video'], 'video', progress_msg),
                    self._download_with_tordl(audio_url, file_paths['audio'], 'audio', progress_msg)
                )
                result = await self._merge_files(file_paths)

            return result
        finally:
            try:
                size = os.path.getsize(result) if result and os.path.exists(result) else 0
                duration = time.time() - start_proc
                avg = size / duration if duration > 0 else 0
                log_action(
                    f"📈 Process: {download_type.upper()} {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
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

    async def _download_with_tordl(self, url, filename, media_type, progress_msg):
        """
        1) Пробуем tor-dl (3 попытки) на одном SOCKS-порту (по умолчанию 9050).
        2) Fallback #1: curl через --socks5-hostname.
        3) Fallback #2: yt-dlp через --proxy socks5://... (если установлен).
        """
        attempts = 0
        max_attempts = 3

        socks_host = os.getenv("TOR_SOCKS_HOST", "127.0.0.1")
        # для curl/yt-dlp используем явный порт из ENV (а не round-robin), дефолт 9050
        socks_port_env = os.getenv("TOR_SOCKS_PORT")
        socks_port = int(socks_port_env) if socks_port_env and socks_port_env.isdigit() else 9050

        # найти tor-dl (сначала системный, затем локальный)
        if platform.system() == 'Windows':
            candidates = ['tor-dl.exe', './tor-dl.exe', '/usr/local/bin/tor-dl.exe']
        else:
            candidates = ['tor-dl', './tor-dl', '/usr/local/bin/tor-dl']
        executable = next((p for p in candidates if os.path.isfile(p)), None)

        try:
            ua = self.user_agent.random
        except Exception:
            ua = 'Mozilla/5.0'

        # ── Основной путь: tor-dl ─────────────────────────────────────────────
        while attempts < max_attempts and executable:
            attempts += 1
            # сам tor-dl пусть получает порт из пула — но пул у нас содержит только то, что в TOR_SOCKS_PORTS (обычно 9050)
            port = await self.port_pool.get_next_port()
            log_action(f"🚀 {media_type.upper()} через порт {port} (попытка {attempts})")

            if not os.access(executable, os.X_OK):
                try:
                    os.chmod(executable, os.stat(executable).st_mode | stat.S_IEXEC)
                    log_action(f"✅ Права на исполнение выданы: {executable}")
                except Exception as e:
                    log_action(f"⚠️ Не удалось выдать права на исполнение {executable}: {e}")

            if not os.path.exists(executable):
                log_action(f"❌ tor-dl не найден по пути: {executable}")
                break

            cmd = [
                executable,
                '--tor-port', str(port),
                '--name', os.path.basename(filename),
                '--destination', os.path.dirname(filename),
                '--circuits', '50',
                '--min-lifetime', '1',
                '--force',
                '--silent',
                url
            ]

            start_time = time.time()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )

            monitor_task = asyncio.create_task(
                self._aggressive_monitor(proc, filename, start_time, media_type)
            )

            done, pending = await asyncio.wait(
                [asyncio.create_task(proc.wait()), monitor_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except Exception:
                    pass

            # Успех, если файл существует и выглядит завершённым
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                if self._is_download_complete(filename, media_type):
                    size = os.path.getsize(filename)
                    duration = time.time() - start_time
                    speed = size / duration if duration > 0 else 0
                    log_action(f"✅ {media_type.upper()}: {size/1024/1024:.1f}MB за {duration:.1f}s ({speed/1024/1024:.1f} MB/s)")
                    return filename
                else:
                    log_action(f"⚠️ {media_type.upper()}: неполная загрузка (tor-dl), ретрай...")

            await asyncio.sleep(1)

        # ── FALLBACK #1: curl через SOCKS5 ────────────────────────────────────
        log_action(f"🛟 {media_type.upper()}: fallback на curl через SOCKS5 {socks_host}:{socks_port}")
        tmp_file = filename + ".part"
        curl_cmd = [
            'curl', '-L', '--fail', '--show-error',
            '--retry', '5', '--retry-delay', '2',
            '--socks5-hostname', f'{socks_host}:{socks_port}',
            '-H', f'User-Agent: {ua}',
            '-o', tmp_file, url
        ]
        proc = await asyncio.create_subprocess_exec(
            *curl_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(tmp_file) and os.path.getsize(tmp_file) > 0:
            os.replace(tmp_file, filename)
            if self._is_download_complete(filename, media_type):
                log_action(f"✅ {media_type.upper()}: скачано curl")
                return filename
            else:
                log_action(f"⚠️ {media_type.upper()}: curl дал неполный файл")
        else:
            try:
                log_action(f"❌ curl ошибка ({proc.returncode}): {err.decode(errors='ignore')[:500]}")
            except Exception:
                pass
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except Exception:
                    pass

        # ── FALLBACK #2: yt-dlp через SOCKS5 (если установлен) ───────────────
        ytdlp = shutil.which('yt-dlp')
        if ytdlp:
            log_action(f"🛟 {media_type.upper()}: fallback на yt-dlp через SOCKS5")
            # Для аудио берём bestaudio/best, для видео — bestvideo (без аудио, т.к. у нас отдельная сборка)
            ytdlp_fmt = 'bestaudio/best' if media_type == 'audio' else 'bestvideo'
            ytdlp_cmd = [
                ytdlp,
                '--no-warnings',
                '--proxy', f'socks5://{socks_host}:{socks_port}',
                '-f', ytdlp_fmt,
                '-o', filename,
                url
            ]
            proc = await asyncio.create_subprocess_exec(
                *ytdlp_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            out, err = await proc.communicate()
            if proc.returncode == 0 and os.path.exists(filename) and os.path.getsize(filename) > 0:
                log_action(f"✅ {media_type.upper()}: скачано yt-dlp")
                return filename
            else:
                try:
                    log_action(f"❌ yt-dlp ошибка ({proc.returncode}): {err.decode(errors='ignore')[:500]}")
                except Exception:
                    pass

        raise Exception(f"Не удалось загрузить {media_type} ни через tor-dl, ни через curl/yt-dlp")

    def _is_download_complete(self, filename, media_type):
        """Грубая проверка, что файл выглядит полным."""
        try:
            size = os.path.getsize(filename)
            # Минимальные размеры для эвристики
            min_audio_size = 1 * 1024 * 1024   # 1 MB
            min_video_size = 10 * 1024 * 1024  # 10 MB
            if media_type == 'audio':
                return size >= min_audio_size
            else:
                return size >= min_video_size
        except Exception:
            return False

    async def _aggressive_monitor(self, proc, filename, start_time, media_type):
        """Агрессивный мониторинг: если 30с нет прогресса — даём перезапуститься внешней логике."""
        last_size = 0
        last_change_time = start_time
        stall_threshold = 30   # 30 сек без изменений = зависание
        check_interval = 3     # проверяем каждые 3 сек
        log_interval = 15      # логируем каждые 15 сек
        last_log_time = start_time

        while proc.returncode is None:
            try:
                await asyncio.sleep(check_interval)
                current_time = time.time()

                if not os.path.exists(filename):
                    continue

                current_size = os.path.getsize(filename)

                if current_size > last_size:
                    last_size = current_size
                    last_change_time = current_time

                    if current_time - last_log_time >= log_interval:
                        elapsed = current_time - start_time
                        speed = current_size / elapsed if elapsed > 0 else 0
                        log_action(f"📊 {media_type}: {current_size/1024/1024:.0f}MB | {speed/1024/1024:.1f} MB/s")
                        last_log_time = current_time
                else:
                    stall_time = current_time - last_change_time
                    if stall_time > stall_threshold:
                        log_action(f"🔄 {media_type}: зависание {stall_time:.0f}с, перезапуск...")
                        return
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _merge_files(self, file_paths):
        log_action("🔄 Объединение видео и аудио...")
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
            log_action(f"❌ FFmpeg error {proc.returncode}: {err.decode(errors='ignore')[:800]}")
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        log_action(f"✅ Output: {file_paths['output']}")
        return file_paths['output']

    async def _cleanup_temp_files(self, file_paths):
        for key in ['video', 'audio']:
            fp = file_paths.get(key)
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                    log_action(f"🧹 Удален временный файл: {fp}")
                except Exception:
                    pass

    async def stop(self):
        """Остановка всех воркеров"""
        if self.is_running:
            self.is_running = False
            for task in self.active_tasks:
                task.cancel()
            await asyncio.gather(*self.active_tasks, return_exceptions=True)
            log_action("🛑 Все воркеры остановлены")


def safe_log(msg):
    tqdm.write(msg)
    log_action(msg)
