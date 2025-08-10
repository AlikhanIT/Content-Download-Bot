#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import platform
import stat
import uuid
import subprocess
import asyncio
import time
from functools import cached_property

from tqdm import tqdm
from fake_useragent import UserAgent

from bot.utils.video_info import get_video_info_with_cache, extract_url_from_info
from bot.utils.log import log_action


def safe_log(msg: str):
    try:
        tqdm.write(msg)
    except Exception:
        pass
    log_action(msg)


class YtDlpDownloader:
    """Скачивание видеодорожки и аудио + склейка через ffmpeg.
    - один тор SOCKS-порт: 9050
    - фолбэк на curl без прокси
    - корректные контейнеры/кодеки при склейке
    """
    _instance = None

    DOWNLOAD_DIR = "/downloads"

    # Запросы качества -> itag видео (H.264/MP4)
    QUALITY_ITAG_MAP = {
        "144": "160",
        "240": "133",
        "360": "134",
        "480": "135",
        "720": "136",
        "1080": "137",
        "1440": "264",
        "2160": "266",
    }

    # По умолчанию: MP4 (H.264) + AAC
    DEFAULT_VIDEO_ITAG = "137"
    DEFAULT_AUDIO_ITAG = "140"  # AAC (m4a) — совместимо с MP4

    # Если видео не MP4 (вдруг выберут WebM/AV1), возьмём Opus
    FALLBACK_OPUS_ITAGS = ["251", "249"]  # webm/opus

    # MP4-видео itags (для выбора совместимого аудио 140)
    MP4_VIDEO_ITAGS = {"137", "136", "135", "134", "133", "160", "18", "22"}

    # Тор-конфиг
    TOR_SOCKS_PORT = 9050  # один-единственный порт
    MAX_RETRIES = 3

    def __new__(cls, max_threads=4, max_queue_size=20):
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
        safe_log(f"📂 Папка для загрузки: {self.DOWNLOAD_DIR}")

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

    async def stop(self):
        if self.is_running:
            self.is_running = False
            for t in list(self.active_tasks):
                t.cancel()
            await asyncio.gather(*self.active_tasks, return_exceptions=True)
            safe_log("🛑 Все воркеры остановлены")

    async def download(self, url, download_type="video", quality="480", progress_msg=None):
        start_time = time.time()
        await self.start_workers()
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        await self.queue.put((url, download_type, quality, fut, progress_msg))
        result_path = await fut

        try:
            if result_path and os.path.exists(result_path):
                size = os.path.getsize(result_path)
                dur = time.time() - start_time
                avg = (size / dur) if dur > 0 else 0
                safe_log(f"📊 Finished: {size / 1024 / 1024:.2f} MB in {dur:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
        except Exception:
            pass
        return result_path

    async def _worker(self):
        while True:
            url, download_type, quality, fut, progress_msg = await self.queue.get()
            try:
                res = await self._process_download(url, download_type, quality, progress_msg)
                fut.set_result(res)
            except Exception as e:
                fut.set_exception(e)
            finally:
                self.queue.task_done()

    async def _process_download(self, url, download_type, quality, progress_msg):
        started = time.time()
        file_paths = await self._prepare_file_paths(download_type)
        output = None

        try:
            info = await get_video_info_with_cache(url)

            if download_type == "audio":
                # Явно скачиваем аудио AAC (140) — совместимо с MP4
                audio_url = await extract_url_from_info(info, [self.DEFAULT_AUDIO_ITAG] + self.FALLBACK_OPUS_ITAGS)
                # Подберём расширение по истинному формату
                audio_itag = self._detect_itag_from_url(audio_url)
                file_paths["audio"] = self._ensure_audio_extension(file_paths["audio"], audio_itag)
                await self._download_media(audio_url, file_paths["audio"], "audio", progress_msg)
                output = file_paths["audio"]
            else:
                # Выбор видео itag
                v_itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)

                # Под выбранное видео подбираем аудио: для MP4 — AAC(140), иначе Opus
                if v_itag in self.MP4_VIDEO_ITAGS:
                    preferred_audio = [self.DEFAULT_AUDIO_ITAG]  # 140
                else:
                    preferred_audio = self.FALLBACK_OPUS_ITAGS[:]  # 251, 249

                video_url, audio_url = await asyncio.gather(
                    extract_url_from_info(info, [v_itag]),
                    extract_url_from_info(info, preferred_audio),
                )

                # Переопределим расширение аудио-файла по реальному itag
                audio_itag = self._detect_itag_from_url(audio_url)
                file_paths["audio"] = self._ensure_audio_extension(file_paths["audio"], audio_itag)

                # Скачиваем параллельно (видео+аудио)
                await asyncio.gather(
                    self._download_media(video_url, file_paths["video"], "video", progress_msg),
                    self._download_media(audio_url, file_paths["audio"], "audio", progress_msg),
                )

                # Склейка
                output = await self._merge_files(file_paths)

            return output
        finally:
            try:
                size = os.path.getsize(output) if output and os.path.exists(output) else 0
                dur = time.time() - started
                avg = (size / dur) if dur > 0 else 0
                safe_log(f"📈 Process: {download_type.upper()} {size/1024/1024:.2f} MB in {dur:.2f}s ({avg/1024/1024:.2f} MB/s)")
            except Exception:
                pass

            if download_type != "audio":
                await self._cleanup_temp_files(file_paths)

    async def _prepare_file_paths(self, download_type):
        rnd = str(uuid.uuid4())
        base = {"output": os.path.join(self.DOWNLOAD_DIR, f"{rnd}.mp4")}
        if download_type == "audio":
            # по умолчанию m4a (если окажется Opus — позже заменим расширение)
            base["audio"] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}.m4a")
        else:
            base["video"] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_video.mp4")
            base["audio"] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_audio.m4a")
        return base

    def _detect_itag_from_url(self, media_url: str) -> str | None:
        # yt-cached urls часто содержат "itag=XXX"
        try:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(media_url).query)
            itag = q.get("itag", [None])[0]
            return itag
        except Exception:
            return None

    def _ensure_audio_extension(self, audio_path: str, audio_itag: str | None) -> str:
        if audio_itag in ("251", "250", "249"):
            # Opus в WebM
            if not audio_path.endswith(".webm"):
                audio_path = audio_path.rsplit(".", 1)[0] + ".webm"
        else:
            # AAC в M4A
            if not audio_path.endswith(".m4a"):
                audio_path = audio_path.rsplit(".", 1)[0] + ".m4a"
        return audio_path

    async def _download_media(self, url: str, filename: str, media_type: str, progress_msg=None):
        """2 попытки: tor-dl через 9050, затем curl без прокси."""
        attempts = 0
        methods = ["tor", "direct"]  # 1) tor-dl  2) curl -L

        while attempts < self.MAX_RETRIES:
            for method in methods:
                attempts += 1
                safe_log(f"🚀 {media_type.upper()} [{method}] (попытка {attempts})")
                ok = await self._try_one_download(url, filename, media_type, method)
                if ok:
                    return filename
            await asyncio.sleep(1)

        raise Exception(f"Не удалось загрузить {media_type} за {self.MAX_RETRIES} попыток")

    async def _try_one_download(self, url: str, filename: str, media_type: str, method: str) -> bool:
        start_time = time.time()

        if method == "tor":
            executable = "/usr/local/bin/tor-dl" if platform.system() != "Windows" else "tor-dl.exe"
            if not os.path.isfile(executable):
                safe_log(f"❌ tor-dl не найден: {executable}")
                return False

            if not os.access(executable, os.X_OK):
                try:
                    os.chmod(executable, os.stat(executable).st_mode | stat.S_IEXEC)
                    safe_log(f"✅ Права на исполнение выданы: {executable}")
                except Exception as e:
                    safe_log(f"❌ Не могу выдать права на исполнение {executable}: {e}")
                    return False

            cmd = [
                executable,
                "--tor-port",
                str(self.TOR_SOCKS_PORT),
                "--name",
                os.path.basename(filename),
                "--destination",
                os.path.dirname(filename),
                "--circuits",
                "20",
                "--min-lifetime",
                "1",
                "--force",
                "--silent",
                url,
            ]
        else:
            # direct через curl
            ua = ""
            try:
                ua = self.user_agent.random
            except Exception:
                ua = "Mozilla/5.0"
            cmd = [
                "curl",
                "-L",
                "-A",
                ua,
                "--connect-timeout",
                "10",
                "--max-time",
                "600",
                "--retry",
                "3",
                "--retry-delay",
                "1",
                "-o",
                filename,
                url,
            ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            # мониторинг зависаний
            monitor_task = asyncio.create_task(self._aggressive_monitor(proc, filename, start_time, media_type))
            done, pending = await asyncio.wait(
                [asyncio.create_task(proc.wait()), monitor_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            # если процесс ещё жив — убьём
            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except Exception:
                    pass

            if os.path.exists(filename) and os.path.getsize(filename) > 0 and self._is_download_complete(filename, media_type):
                size = os.path.getsize(filename)
                dur = time.time() - start_time
                spd = (size / dur) if dur > 0 else 0
                safe_log(f"✅ {media_type.upper()}: {size/1024/1024:.1f}MB за {dur:.1f}s ({spd/1024/1024:.1f} MB/s)")
                return True

        except Exception as e:
            safe_log(f"❌ Ошибка {media_type} [{method}]: {e}")

        return False

    def _is_download_complete(self, filename: str, media_type: str) -> bool:
        try:
            size = os.path.getsize(filename)
            # нижние пороги, чтобы не считать ~пустой файл успешным
            min_audio = 300 * 1024  # 0.3MB
            min_video = 2 * 1024 * 1024  # 2MB
            return size >= (min_audio if media_type == "audio" else min_video)
        except Exception:
            return False

    async def _aggressive_monitor(self, proc, filename, start_time, media_type):
        """Быстрое выявление зависания."""
        last_size = 0
        last_change = start_time
        stall_threshold = 30  # сек без роста файла
        check_interval = 3
        log_interval = 15
        last_log = start_time

        while proc.returncode is None:
            try:
                await asyncio.sleep(check_interval)
                now = time.time()

                if os.path.exists(filename):
                    sz = os.path.getsize(filename)
                    if sz > last_size:
                        last_size = sz
                        last_change = now

                        if now - last_log >= log_interval:
                            elapsed = now - start_time
                            spd = (sz / elapsed) if elapsed > 0 else 0
                            safe_log(f"📊 {media_type}: {sz/1024/1024:.0f}MB | {spd/1024/1024:.1f} MB/s")
                            last_log = now
                    else:
                        if now - last_change > stall_threshold:
                            safe_log(f"🔄 {media_type}: зависание {(now - last_change):.0f}с, перезапуск…")
                            return
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _merge_files(self, file_paths):
        safe_log("🔄 Объединение видео и аудио…")

        video = file_paths["video"]
        audio = file_paths["audio"]
        output = file_paths["output"]

        audio_ext = os.path.splitext(audio)[1].lower()

        # 1-я попытка: если аудио m4a (AAC) — copy-copy в mp4
        # если webm/opus — сразу перекодируем аудио в AAC, чтобы оставить mp4
        if audio_ext == ".webm":
            cmd = [
                "ffmpeg",
                "-i",
                video,
                "-i",
                audio,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-movflags",
                "+faststart",
                "-y",
                output,
            ]
            rc, out, err = await self._run_ffmpeg(cmd)
            if rc != 0:
                safe_log(f"❌ FFmpeg error {rc}: {err}")
                raise subprocess.CalledProcessError(rc, cmd, out, err)
        else:
            # ожидаем совместимость — пробуем copy-copy
            cmd = [
                "ffmpeg",
                "-i",
                video,
                "-i",
                audio,
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-movflags",
                "+faststart",
                "-y",
                output,
            ]
            rc, out, err = await self._run_ffmpeg(cmd)
            if rc != 0:
                # фолбэк: перекодируем аудио → AAC
                safe_log("⚠️ copy не удался — перекодируем аудио в AAC…")
                cmd2 = [
                    "ffmpeg",
                    "-i",
                    video,
                    "-i",
                    audio,
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-movflags",
                    "+faststart",
                    "-y",
                    output,
                ]
                rc2, out2, err2 = await self._run_ffmpeg(cmd2)
                if rc2 != 0:
                    safe_log(f"❌ FFmpeg error {rc2}: {err2}")
                    raise subprocess.CalledProcessError(rc2, cmd2, out2, err2)

        safe_log(f"✅ Output: {output}")
        return output

    async def _run_ffmpeg(self, cmd):
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode, (out.decode(errors="ignore") if out else ""), (err.decode(errors="ignore") if err else "")

    async def _cleanup_temp_files(self, file_paths):
        for k in ("video", "audio"):
            p = file_paths.get(k)
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    safe_log(f"🧹 Удален временный файл: {p}")
                except Exception:
                    pass
