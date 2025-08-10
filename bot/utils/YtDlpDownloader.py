#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
YtDlpDownloader — быстрый и устойчивый загрузчик (video/audio) c YouTube
через прямые HTTP(S) ссылки форматов yt-dlp + tor-dl для обхода блокировок.

Ключевые фичи:
  • Выбор форматов без HLS/DASH: только direct http(s) (protocol in {http,https})
  • Надёжная валидация скачанных файлов: ffprobe + проверка ожидаемого размера (≥98%)
  • Адаптивное число цепочек tor-dl: audio=1, video=6 (для googlevideo), иначе умеренно
  • Сохранение времёнок сразу с корректным расширением (без угадываний)
  • Сборка через ffmpeg в 4 шага: MP4(copy,+faststart) → MKV(copy) → MP4(copy+AAC) → x264+AAC
  • Чистый клинап временных файлов
  • Мягкий монитор «зависаний» с авто-перезапуском

ENV-переменные (опционально):
  TOR_PORTS="9050,9051,9052"   — список локальных портов Tor (по умолчанию: "9050")
  TOR_DL_BIN="/app/tor-dl"     — путь к исполняемому файлу tor-dl (auto: ./tor-dl[.exe])
  TOR_CIRCUITS_VIDEO="6"       — число цепочек для видеопотоков googlevideo (по умолчанию 6)
  TOR_CIRCUITS_AUDIO="1"       — число цепочек для аудиопотоков googlevideo (по умолчанию 1)
  TOR_CIRCUITS_DEFAULT="4"     — число цепочек для других хостов (по умолчанию 4)
  DOWNLOAD_DIR="/downloads"    — папка загрузок (по умолчанию /downloads)

Зависимости:
  ffmpeg, ffprobe — в PATH
  tor-dl          — используемый бинарь
  pip: fake_useragent, tqdm
  проект: bot.utils.video_info.get_video_info_with_cache, bot.utils.log.log_action
"""

import os
import platform
import stat
import uuid
import subprocess
import asyncio
import time
import json
import shutil
from functools import cached_property
from typing import Dict, List, Optional

from fake_useragent import UserAgent
from tqdm import tqdm

# ваши утилиты проекта
from bot.utils.video_info import get_video_info_with_cache
from bot.utils.log import log_action


def safe_log(msg: str):
    tqdm.write(msg)
    log_action(msg)


# -------------------- Формат-помощники -------------------- #

# Кандидаты для каждой целевой высоты (сначала h264/mp4, затем vp9/webm, затем av1)
VIDEO_ITAG_CANDIDATES: Dict[int, List[str]] = {
    2160: ["266", "401", "315", "272"],  # h264, av1/vp9 варианты
    1440: ["264", "400", "308", "271"],
    1080: ["137", "399", "248", "614", "616", "270"],
    720:  ["136", "398", "247", "232", "609"],
    480:  ["135", "397", "244", "231", "606"],
    360:  ["134", "396", "243", "230", "605", "18"],
    240:  ["133", "395", "242", "229", "604"],
    144:  ["160", "394", "278", "269", "603"],
}

# Аудио: m4a (AAC) предпочтительнее для стабильного mux в MP4
AUDIO_ITAG_PREFERRED: List[str] = ["140", "141", "139", "251", "250", "249"]

DIRECT_PROTOCOLS = {"https", "http"}


def _formats_from_info(info: dict) -> List[dict]:
    fmts = list(info.get("formats") or [])
    requested = info.get("requested_formats") or []
    for rf in requested:
        if rf and isinstance(rf, dict):
            fmts.append(rf)
    # удалим дубликаты по format_id сохраняя последний (обычно requested точнее)
    uniq = {}
    for f in fmts:
        fid = str(f.get("format_id"))
        uniq[fid] = f
    return list(uniq.values())


def _is_direct(fmt: dict) -> bool:
    proto = (fmt.get("protocol") or "").lower()
    return (proto in DIRECT_PROTOCOLS) and bool(fmt.get("url"))


def _fmt_height(fmt: dict) -> int:
    try:
        return int(fmt.get("height") or 0)
    except Exception:
        return 0


def _fmt_ext(fmt: dict) -> str:
    return (fmt.get("ext") or "").lower()


def _fmt_vc(fmt: dict) -> str:
    return (fmt.get("vcodec") or "").lower()


def _fmt_ac(fmt: dict) -> str:
    return (fmt.get("acodec") or "").lower()


def _expected_size(fmt: dict) -> int:
    try:
        return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
    except Exception:
        return 0


def _pick_by_itag_list(fmts: List[dict], itags: List[str]) -> Optional[dict]:
    by_id = {str(f.get("format_id")): f for f in fmts}
    for it in itags:
        f = by_id.get(str(it))
        if f and _is_direct(f):
            return f
    return None


def _pick_best_video_by_height(fmts: List[dict], target_h: int) -> Optional[dict]:
    # только видеопотоки (есть vcodec, нет аудио)
    candidates = [
        f for f in fmts
        if _is_direct(f) and _fmt_vc(f) != "none" and _fmt_ac(f) in ("", "none", None)
    ]
    if not candidates:
        return None

    def key(f):
        h = _fmt_height(f)
        over = 0 if h <= target_h else 1
        dist = abs(target_h - h)
        vc = _fmt_vc(f)
        pref = 0 if ("avc" in vc or "h264" in vc) else (1 if "vp9" in vc else 2)
        # bitrate выше — лучше
        tbr = 0
        try:
            tbr = int(f.get("tbr") or 0)
        except Exception:
            pass
        return (over, dist, pref, -tbr)

    candidates.sort(key=key)
    return candidates[0]


def _pick_best_audio(fmts: List[dict]) -> Optional[dict]:
    by_id = {str(f.get("format_id")): f for f in fmts}
    for it in AUDIO_ITAG_PREFERRED:
        f = by_id.get(it)
        if f and _is_direct(f) and _fmt_ac(f) != "none" and _fmt_vc(f) in ("", "none", None):
            return f

    candidates = [
        f for f in fmts
        if _is_direct(f) and _fmt_ac(f) != "none" and _fmt_vc(f) in ("", "none", None)
    ]
    if not candidates:
        return None
    def abr(f):
        try:
            return int(f.get("abr") or f.get("tbr") or 0)
        except Exception:
            return 0
    candidates.sort(key=lambda f: abr(f), reverse=True)
    return candidates[0]


# -------------------- FFmpeg/ffprobe helpers -------------------- #

def _have_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def _probe_valid(path: str) -> bool:
    """Проверяем, что контейнер читается и есть хотя бы один поток."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    if not _have_ffprobe():
        # Грубая эвристика на случай отсутствия ffprobe: файл не HTML
        try:
            with open(path, "rb") as f:
                head = f.read(1024)
            h = head.lower()
            return (b"<html" not in h) and (b"<!doctype html" not in h)
        except Exception:
            return False

    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25
        )
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout.decode("utf-8", "ignore") or "{}")
        streams = data.get("streams") or []
        return len(streams) > 0
    except Exception:
        return False


# -------------------- Пул портов -------------------- #

class PortPool:
    def __init__(self, ports: List[int]):
        assert ports, "PortPool requires at least one port"
        self._ports = ports[:]
        self._current_index = 0
        self._lock = asyncio.Lock()

    async def get_next_port(self) -> int:
        async with self._lock:
            port = self._ports[self._current_index]
            self._current_index = (self._current_index + 1) % len(self._ports)
            return port


# -------------------- Основной загрузчик -------------------- #

class YtDlpDownloader:
    _instance = None

    # дефолтные пределы
    MAX_RETRIES = 10
    DEFAULT_AUDIO_ITAG = "140"

    def __new__(cls, max_threads: int = 8, max_queue_size: int = 20):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize(max_threads, max_queue_size)
            cls._instance._ensure_download_dir()
            cls._instance._selfcheck_binaries()
        return cls._instance

    def _initialize(self, max_threads: int, max_queue_size: int):
        self.max_threads = max_threads
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self.is_running = False
        self.active_tasks: set = set()

        # Порты Tor из ENV
        ports_env = os.getenv("TOR_PORTS", "9050")
        try:
            ports = [int(p.strip()) for p in ports_env.split(",") if p.strip()]
        except Exception:
            ports = [9050]
        self.port_pool = PortPool(ports)

        # Папка загрузок из ENV
        self.DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")

        # tor-dl путь из ENV (или авто)
        self.tor_dl_override = os.getenv("TOR_DL_BIN", "").strip() or None

        # circuits настройки
        self.circuits_video = int(os.getenv("TOR_CIRCUITS_VIDEO", "6"))
        self.circuits_audio = int(os.getenv("TOR_CIRCUITS_AUDIO", "1"))
        self.circuits_default = int(os.getenv("TOR_CIRCUITS_DEFAULT", "4"))

    def _ensure_download_dir(self):
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        safe_log(f"📂 Папка для загрузки: {self.DOWNLOAD_DIR}")

    def _selfcheck_binaries(self):
        # ffmpeg / ffprobe
        if shutil.which("ffmpeg") is None:
            safe_log("⚠️ ffmpeg не найден в PATH — перекодирование может упасть.")
        if shutil.which("ffprobe") is None:
            safe_log("⚠️ ffprobe не найден в PATH — валидатор контейнера ограничен.")
        # tor-dl — проверим существование когда будем запускать

    @cached_property
    def user_agent(self):
        # пока не используется, но оставим для будущего (подмены UA при прямых закачках)
        return UserAgent()

    # ---------- Публичные методы ---------- #

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
            for task in list(self.active_tasks):
                task.cancel()
            await asyncio.gather(*list(self.active_tasks), return_exceptions=True)
            safe_log("🛑 Все воркеры остановлены")

    async def download(self, url: str, download_type: str = "video", quality: str = "480", progress_msg=None) -> str:
        start_time = time.time()
        await self.start_workers()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        await self.queue.put((url, download_type, quality, fut, progress_msg))
        result = await fut
        try:
            size = os.path.getsize(result)
            duration = time.time() - start_time
            avg = size / duration if duration > 0 else 0
            safe_log(f"📊 Finished: {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
        except Exception:
            pass
        return result

    # ---------- Внутренности очереди ---------- #

    async def _worker(self):
        while True:
            url, download_type, quality, future, progress_msg = await self.queue.get()
            try:
                res = await self._process_download(url, download_type, quality, progress_msg)
                if not future.cancelled():
                    future.set_result(res)
            except Exception as e:
                if not future.cancelled():
                    future.set_exception(e)
            finally:
                self.queue.task_done()

    async def _process_download(self, url: str, download_type: str, quality: str, progress_msg) -> str:
        start_proc = time.time()
        temp_paths = await self._prepare_temp_paths()
        result: Optional[str] = None

        try:
            try:
                target_h = int(quality)
            except Exception:
                target_h = 480

            info = await get_video_info_with_cache(url)
            fmts = _formats_from_info(info)

            if download_type == "audio":
                a_fmt = _pick_best_audio(fmts)
                if not a_fmt:
                    raise Exception("❌ Не найден подходящий аудиопоток (direct).")

                aext = _fmt_ext(a_fmt) or ("m4a" if "aac" in _fmt_ac(a_fmt) else "webm")
                audio_path = temp_paths["audio"] + f".{aext}"

                await self._download_with_tordl(
                    a_fmt["url"], audio_path, "audio", progress_msg,
                    expected_size=_expected_size(a_fmt)
                )
                result = audio_path

            else:
                # VIDEO
                cand_itags = VIDEO_ITAG_CANDIDATES.get(target_h) or []
                v_fmt = _pick_by_itag_list(fmts, cand_itags) or _pick_best_video_by_height(fmts, target_h)
                if not v_fmt:
                    raise Exception(f"❌ Не найден подходящий видеопоток для {target_h}p (direct).")

                # AUDIO
                a_fmt = _pick_best_audio(fmts)
                if not a_fmt:
                    raise Exception("❌ Не найден подходящий аудиопоток (direct).")

                vext = _fmt_ext(v_fmt) or ("mp4" if ("avc" in _fmt_vc(v_fmt) or "h264" in _fmt_vc(v_fmt)) else "webm")
                aext = _fmt_ext(a_fmt) or ("m4a" if "aac" in _fmt_ac(a_fmt) else "webm")
                v_path = temp_paths["video"] + f".{vext}"
                a_path = temp_paths["audio"] + f".{aext}"

                await asyncio.gather(
                    self._download_with_tordl(v_fmt["url"], v_path, "video", progress_msg, expected_size=_expected_size(v_fmt)),
                    self._download_with_tordl(a_fmt["url"], a_path, "audio", progress_msg, expected_size=_expected_size(a_fmt)),
                )
                result = await self._merge_files(
                    {"video": v_path, "audio": a_path, "output_base": temp_paths["output_base"]},
                    v_fmt, a_fmt
                )

            return result

        finally:
            try:
                size = os.path.getsize(result) if result and os.path.exists(result) else 0
                duration = time.time() - start_proc
                avg = size / duration if duration > 0 else 0
                safe_log(f"📈 Process: {download_type.upper()} {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
            except Exception:
                pass
            await self._cleanup_temp_files(temp_paths, preserve=result)

    # ---------- Скачивание через tor-dl ---------- #

    def _resolve_tor_dl_path(self) -> str:
        if self.tor_dl_override:
            return self.tor_dl_override
        if platform.system() == "Windows":
            return "./tor-dl.exe"
        return "./tor-dl"

    def _pick_circuits(self, host: str, media_type: str) -> int:
        h = (host or "").lower()
        if "googlevideo" in h or "youtube" in h:
            return self.circuits_audio if media_type == "audio" else self.circuits_video
        return self.circuits_default

    async def _download_with_tordl(self, url: str, filename: str, media_type: str, progress_msg, expected_size: int = 0) -> str:
        """
        Закачка через tor-dl с ретраями и валидацией.
        Для googlevideo:
           - audio: circuits=1
           - video: circuits=6
        Для других хостов — circuits=4 по умолчанию.
        """
        attempts = 0
        max_attempts = 4

        # выбор числа цепочек по хосту
        host = ""
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            pass

        circuits = self._pick_circuits(host, media_type)

        while attempts < max_attempts:
            attempts += 1
            port = await self.port_pool.get_next_port()
            safe_log(f"🚀 {media_type.upper()} через порт {port} (попытка {attempts}, circuits={circuits})")

            executable = self._resolve_tor_dl_path()
            if not os.path.isfile(executable):
                raise FileNotFoundError(f"❌ Файл не найден: {executable}")

            if not os.access(executable, os.X_OK):
                os.chmod(executable, os.stat(executable).st_mode | stat.S_IEXEC)
                safe_log(f"✅ Права на исполнение выданы: {executable}")

            # гарантируем чистый старт файла
            try:
                if os.path.exists(filename):
                    os.remove(filename)
            except Exception:
                pass

            # имя файла, видимое tor-dl
            tor_name = os.path.basename(filename)
            tor_dest = os.path.dirname(os.path.abspath(filename)) or "."

            cmd = [
                executable,
                "--tor-port", str(port),
                "--name", tor_name,
                "--destination", tor_dest,
                "--circuits", str(circuits),
                "--min-lifetime", "20",
                "--force",
                "--silent",
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

            try:
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

                # гарантированно завершим дочерний процесс
                if proc.returncode is None:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except Exception:
                        pass

                # проверяем файл
                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    if self._is_download_complete(filename, media_type, expected_size):
                        size = os.path.getsize(filename)
                        duration = time.time() - start_time
                        speed = size / duration if duration > 0 else 0
                        safe_log(f"✅ {media_type.upper()}: {size / 1024 / 1024:.1f}MB за {duration:.1f}s ({speed / 1024 / 1024:.1f} MB/s)")
                        return filename
                    else:
                        safe_log(f"⚠️ {media_type.upper()}: файл неполный/бракованный, перезапуск...")

            except Exception as e:
                safe_log(f"❌ Ошибка {media_type} попытка {attempts}: {e}")
                try:
                    if proc.returncode is None:
                        proc.kill()
                except Exception:
                    pass

            await asyncio.sleep(1)

        raise Exception(f"Не удалось загрузить {media_type} за {max_attempts} попыток")

    def _is_download_complete(self, filename: str, media_type: str, expected_size: int = 0) -> bool:
        try:
            size = os.path.getsize(filename)
            min_audio_size = 1 * 1024 * 1024      # 1MB
            min_video_size = 10 * 1024 * 1024     # 10MB
            floor = min_audio_size if media_type == "audio" else min_video_size

            if expected_size > 0:
                if size < max(floor, int(expected_size * 0.98)):
                    return False
                return _probe_valid(filename)

            if size < floor:
                return False
            return _probe_valid(filename)
        except Exception:
            return False

    async def _aggressive_monitor(self, proc, filename: str, start_time: float, media_type: str):
        last_size = 0
        last_change_time = start_time
        stall_threshold = 45  # было 30, дали больше времени
        check_interval = 3
        log_interval = 15
        last_log_time = start_time

        while True:
            if proc.returncode is not None:
                break
            try:
                await asyncio.sleep(check_interval)
                current_time = time.time()

                if os.path.exists(filename):
                    current_size = os.path.getsize(filename)
                    if current_size > last_size:
                        last_size = current_size
                        last_change_time = current_time
                        if current_time - last_log_time >= log_interval:
                            elapsed = current_time - start_time
                            speed = current_size / elapsed if elapsed > 0 else 0
                            safe_log(f"📊 {media_type}: {current_size / 1024 / 1024:.0f}MB | {speed / 1024 / 1024:.1f} MB/s")
                            last_log_time = current_time
                    else:
                        if (current_time - last_change_time) > stall_threshold:
                            safe_log(f"🔄 {media_type}: зависание {(current_time - last_change_time):.0f}с, перезапуск...")
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            return
            except asyncio.CancelledError:
                break
            except Exception:
                # не ломаемся из-за мониторинга
                pass

    # ---------- Сборка видео+аудио ---------- #

    async def _merge_files(self, paths: Dict[str, str], v_fmt: Optional[dict] = None, a_fmt: Optional[dict] = None) -> str:
        vcodec = _fmt_vc(v_fmt or {})
        acodec = _fmt_ac(a_fmt or {})
        video_path = paths["video"]
        audio_path = paths["audio"]

        def run_ffmpeg(cmd: List[str]):
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return proc.returncode, proc.stdout, proc.stderr

        # 1) MP4 + copy (+faststart)
        output_mp4 = paths["output_base"] + ".mp4"
        if ("avc" in vcodec or "h264" in vcodec) and ("mp4a" in acodec or "aac" in acodec or audio_path.endswith(".m4a")):
            cmd1 = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", video_path, "-i", audio_path,
                "-c:v", "copy", "-c:a", "copy",
                "-map", "0:v:0", "-map", "1:a:0",
                "-movflags", "+faststart",
                "-y", output_mp4
            ]
            rc, _, err = run_ffmpeg(cmd1)
            if rc == 0 and os.path.exists(output_mp4) and os.path.getsize(output_mp4) > 0:
                safe_log(f"✅ Output: {output_mp4}")
                return output_mp4
            else:
                safe_log(f"⚠️ MP4 copy не удалось, пробуем MKV. FFmpeg: {err.decode(errors='ignore')[:300]}")

        # 2) MKV + copy (универсально)
        output_mkv = paths["output_base"] + ".mkv"
        cmd2 = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", video_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "copy",
            "-map", "0:v:0", "-map", "1:a:0",
            "-y", output_mkv
        ]
        rc, _, err = run_ffmpeg(cmd2)
        if rc == 0 and os.path.exists(output_mkv) and os.path.getsize(output_mkv) > 0:
            safe_log(f"✅ Output: {output_mkv}")
            return output_mkv
        else:
            safe_log(f"⚠️ MKV copy не удалось, пробуем перекодирование аудио. FFmpeg: {err.decode(errors='ignore')[:300]}")

        # 3) copy+AAC (если видео уже h264)
        if "avc" in vcodec or "h264" in vcodec:
            output_mp4_aac = paths["output_base"] + ".mp4"
            cmd3 = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", video_path, "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-movflags", "+faststart",
                "-y", output_mp4_aac
            ]
            rc, _, err = run_ffmpeg(cmd3)
            if rc == 0 and os.path.exists(output_mp4_aac) and os.path.getsize(output_mp4_aac) > 0:
                safe_log(f"✅ Output: {output_mp4_aac}")
                return output_mp4_aac
            else:
                safe_log(f"⚠️ copy+AAC не удалось, пробуем полный транскод. FFmpeg: {err.decode(errors='ignore')[:300]}")

        # 4) Полный транскод
        output_mp4_full = paths["output_base"] + ".mp4"
        cmd4 = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", video_path, "-i", audio_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0",
            "-movflags", "+faststart",
            "-y", output_mp4_full
        ]
        rc, out, err = run_ffmpeg(cmd4)
        if rc != 0:
            safe_log(f"❌ FFmpeg error {rc}: {err.decode(errors='ignore')[:500]}")
            raise subprocess.CalledProcessError(rc, cmd4, out, err)
        safe_log(f"✅ Output: {output_mp4_full}")
        return output_mp4_full

    # ---------- Временные файлы ---------- #

    async def _prepare_temp_paths(self) -> Dict[str, str]:
        rnd = uuid.uuid4()
        base = {
            "video": os.path.join(self.DOWNLOAD_DIR, f"{rnd}_video"),
            "audio": os.path.join(self.DOWNLOAD_DIR, f"{rnd}_audio"),
            "output_base": os.path.join(self.DOWNLOAD_DIR, f"{rnd}"),
        }
        return base

    async def _cleanup_temp_files(self, paths: Dict[str, str], preserve: Optional[str]):
        """Удаляем времёнки. Итоговый файл (preserve) не трогаем."""
        keep = os.path.abspath(preserve) if preserve else None
        for k in ("video", "audio"):
            fp = paths.get(k)
            if not fp:
                continue
            # возможные варианты расширений
            candidates = [fp, fp + ".mp4", fp + ".webm", fp + ".m4a", fp + ".mkv", fp + ".mp3", fp + ".mov"]
            for cand in candidates:
                try:
                    if cand and os.path.exists(cand):
                        if keep and os.path.abspath(cand) == keep:
                            continue
                        os.remove(cand)
                        safe_log(f"🧹 Удален временный файл: {cand}")
                except Exception:
                    pass
