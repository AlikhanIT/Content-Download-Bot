#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
YtDlpDownloader (v2.6)
— авто-TOR для googlevideo в фолбэке (фикс 403 при несоответствии IP),
— быстрый фолбэк для аудио (curl/wget),
— -force для tor-dl (нет ошибки "videoplayback already exists"),
— аккуратные имена (-n/-d) и реалтайм-логи,
— строгая последовательность (СНАЧАЛА ВИДЕО → затем АУДИО),
— авто-решение: при 403 от curl/wget без TOR мгновенно повторяем через TOR.

Внешние контракты сохранены:
  - класс: YtDlpDownloader (singleton)
  - методы: start_workers(), stop(), download(url, download_type="video", quality="480", progress_msg=None)

ENV (все опциональны):
  YT_MAX_THREADS, YT_QUEUE_SIZE

  TOR_DL_BIN                 — путь к tor-dl (если не в PATH)
  TOR_SOCKS_PORT             — единый SOCKS-порт (default 9050)

  TOR_CIRCUITS_VIDEO         — по умолчанию 6
  TOR_CIRCUITS_AUDIO         — по умолчанию 4   (⬆️ было 1/3)
  TOR_CIRCUITS_DEFAULT       — по умолчанию 4

  YT_FALLBACK                — 1/0 (включить/выключить фолбэк curl/wget), default=1
  YT_FALLBACK_TOR            — "auto" (по умолчанию), 1 (всегда через TOR), 0 (сначала без TOR)
  YT_AUDIO_PREFER_CURL       — 1 (по умолчанию) — сначала curl/wget, потом tor-dl, 0 — наоборот

  TOR_DL_RPS, TOR_DL_SEGMENT_SIZE, TOR_DL_SEGMENT_RETRIES, TOR_DL_MIN_LIFETIME,
  TOR_DL_RETRY_BASE_MS, TOR_DL_TAIL_THRESHOLD, TOR_DL_TAIL_WORKERS,
  TOR_DL_TAIL_SHARD_MIN, TOR_DL_TAIL_SHARD_MAX,
  TOR_DL_ALLOW_HTTP (1/0), TOR_DL_VERBOSE (1/0), TOR_DL_QUIET (1/0), TOR_DL_SILENT (1/0),
  TOR_DL_UA (UA строка), TOR_DL_REFERER (по умолчанию https://www.youtube.com/)

  DOWNLOAD_DIR               — папка для временных/выходных файлов (default /downloads)
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
import re
import shlex
import unicodedata
from functools import cached_property
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from fake_useragent import UserAgent
from tqdm import tqdm

# внешние утилиты (контракт сохранён)
from bot.utils.video_info import get_video_info_with_cache
from bot.utils.log import log_action


# -------------------- Logging helpers -------------------- #

def safe_log(msg: str):
    try:
        tqdm.write(msg)
    finally:
        try:
            log_action(msg)
        except Exception:
            pass


def _quote_arg(arg: str) -> str:
    if platform.system() == "Windows":
        need = any(ch.isspace() or ch in '"&|^<>()[]{}=;,' for ch in arg)
        if '"' in arg:
            arg = arg.replace('"', '\\"')
        return f'"{arg}"' if need or arg == "" else arg
    return shlex.quote(arg)


def _join_cmd_for_log(cmd: List[str]) -> str:
    return " ".join(_quote_arg(a) for a in cmd)


# -------------------- Filename helpers -------------------- #

_WINDOWS_FORBIDDEN = set('<>:"/\\|?*')
_POSIX_FORBIDDEN = set('/')

def _sanitize_component(name: str, keep_unicode: bool = True) -> str:
    name = name.strip()
    if not keep_unicode:
        name = unicodedata.normalize("NFKD", name)
        name = name.encode("ascii", "ignore").decode("ascii")
    # заменить запрещённые символы
    bad = _WINDOWS_FORBIDDEN if platform.system() == "Windows" else _POSIX_FORBIDDEN
    name = "".join(ch if ch not in bad else "_" for ch in name)
    # убрать управляющие
    name = "".join(ch if ch.isprintable() else "_" for ch in name)
    # обрезать длину
    return name[:180] or "file"


def _make_names(info: dict, v_fmt: Optional[dict], a_fmt: Optional[dict], target_h: int) -> Dict[str, str]:
    title = info.get("title") or "video"
    safe_title = _sanitize_component(title, keep_unicode=True)

    def ext_from_v(fmt: Optional[dict]) -> str:
        if not fmt: return "mp4"
        ext = (fmt.get("ext") or "").lower()
        if not ext:
            vc = (fmt.get("vcodec") or "").lower()
            ext = "mp4" if ("avc" in vc or "h264" in vc) else "webm"
        return ext

    def ext_from_a(fmt: Optional[dict]) -> str:
        if not fmt: return "m4a"
        ext = (fmt.get("ext") or "").lower()
        if not ext:
            ac = (fmt.get("acodec") or "").lower()
            ext = "m4a" if ("aac" in ac or "mp4a" in ac) else "webm"
        return ext

    vh = 0
    if v_fmt:
        try:
            vh = int(v_fmt.get("height") or 0)
        except Exception:
            vh = 0
    if vh <= 0:
        vh = int(target_h)

    names = {
        "video_fn": f"{safe_title} [V{vh}].{ext_from_v(v_fmt)}",
        "audio_fn": f"{safe_title} [AUDIO].{ext_from_a(a_fmt)}",
        "prog_fn":  f"{safe_title} [P{vh}].{ext_from_v(v_fmt)}",
        "merge_base": f"{safe_title} [V{vh}]",
    }
    return names


# -------------------- Format helpers -------------------- #

DIRECT_PROTOCOLS = {"https", "http"}

VIDEO_ITAG_CANDIDATES: Dict[int, List[str]] = {
    2160: ["266", "401", "315", "272"],
    1440: ["264", "400", "308", "271"],
    1080: ["137", "399", "248", "614", "616", "270"],
    720:  ["136", "398", "247", "232", "609", "22", "18"],
    480:  ["135", "397", "244", "231", "606", "18"],
    360:  ["134", "396", "243", "230", "605", "18"],
    240:  ["133", "395", "242", "229", "604"],
    144:  ["160", "394", "278", "269", "603"],
}
PROGRESSIVE_PREFERENCE = ("mp4", "mov", "m4v", "webm")
AUDIO_ITAG_PREFERRED: List[str] = ["140", "141", "139", "251", "250", "249"]


def _formats_from_info(info: dict) -> List[dict]:
    fmts = list(info.get("formats") or [])
    requested = info.get("requested_formats") or []
    for rf in requested:
        if rf and isinstance(rf, dict):
            fmts.append(rf)
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


def _fmt_tbr(fmt: dict) -> int:
    try:
        return int(fmt.get("tbr") or fmt.get("abr") or 0)
    except Exception:
        return 0


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
    """Абсолютная близость к target_h; при равенстве — предпочитаем AVC/H.264 и больший TBR."""
    candidates = [f for f in fmts if _is_direct(f) and _fmt_vc(f) != "none" and _fmt_ac(f) in ("", "none", None)]
    if not candidates:
        return None

    def codec_pref(vc: str) -> int:
        vc = vc or ""
        if "avc" in vc or "h264" in vc:
            return 0
        if "vp9" in vc:
            return 1
        return 2

    def key(f):
        h = _fmt_height(f)
        dist = abs(target_h - h) if h > 0 else 10_000
        pref = codec_pref(_fmt_vc(f))
        tbr = _fmt_tbr(f)
        return (dist, pref, -tbr)

    candidates.sort(key=key)
    return candidates[0]


def _pick_best_audio(fmts: List[dict]) -> Optional[dict]:
    by_id = {str(f.get("format_id")): f for f in fmts}
    for it in AUDIO_ITAG_PREFERRED:
        f = by_id.get(it)
        if f and _is_direct(f) and _fmt_ac(f) != "none" and _fmt_vc(f) in ("", "none", None):
            return f
    candidates = [f for f in fmts if _is_direct(f) and _fmt_ac(f) != "none" and _fmt_vc(f) in ("", "none", None)]
    if not candidates:
        return None
    candidates.sort(key=lambda f: _fmt_tbr(f), reverse=True)
    return candidates[0]


def _pick_best_progressive(fmts: List[dict], target_h: int) -> Optional[dict]:
    """Абсолютная близость к target_h; при равенстве — предпочтение формату из PROGRESSIVE_PREFERENCE и большему TBR."""
    candidates = [f for f in fmts if _is_direct(f) and _fmt_vc(f) != "none" and _fmt_ac(f) != "none"]
    if not candidates:
        return None

    def pref_ext(ext: str) -> int:
        try:
            return PROGRESSIVE_PREFERENCE.index(ext)
        except ValueError:
            return len(PROGRESSIVE_PREFERENCE)

    def key(f):
        h = _fmt_height(f)
        dist = abs(target_h - h) if h > 0 else 10_000
        pext = pref_ext(_fmt_ext(f))
        tbr = _fmt_tbr(f)
        return (dist, pext, -tbr)

    candidates.sort(key=key)
    return candidates[0]


def _fmt_info(tag: str, fmt: Optional[dict]) -> str:
    if not fmt:
        return f"{tag}: none"
    return (f"{tag}: itag={fmt.get('format_id')} "
            f"{_fmt_height(fmt)}p {_fmt_ext(fmt)} "
            f"v={_fmt_vc(fmt) or '—'} a={_fmt_ac(fmt) or '—'} "
            f"tbr={_fmt_tbr(fmt)}")


# -------------------- FFmpeg/ffprobe helpers -------------------- #

def _have_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def _probe_valid(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    if not _have_ffprobe():
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


# -------------------- Utilities -------------------- #

def _url_requires_tor(url: str) -> bool:
    """Если ссылка привязана к конкретному IP (как у googlevideo: ip=..., ipbypass=yes),
    фолбэк без TOR даст 403. В таком случае сразу идём через TOR."""
    try:
        u = urlparse(url or "")
        host = (u.netloc or "").lower()
        qs = parse_qs(u.query or "")
        if "googlevideo.com" in host:
            if "ip" in qs or (qs.get("ipbypass", [""])[0] or "").lower() in ("1", "yes", "true"):
                return True
        return False
    except Exception:
        return False


# -------------------- Downloader -------------------- #

class YtDlpDownloader:
    _instance = None
    MAX_RETRIES = 10

    def __new__(cls, max_threads: int = None, max_queue_size: int = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize(max_threads, max_queue_size)
            cls._instance._ensure_download_dir()
            cls._instance._selfcheck_binaries()
        return cls._instance

    def _initialize(self, max_threads: Optional[int], max_queue_size: Optional[int]):
        if max_threads is None:
            try:
                cpu_cnt = os.cpu_count() or 4
            except Exception:
                cpu_cnt = 4
            max_threads = int(os.getenv("YT_MAX_THREADS", cpu_cnt))
        max_threads = max(1, min(max_threads, 16))
        self.max_threads = max_threads

        if max_queue_size is None:
            try:
                mq = int(os.getenv("YT_QUEUE_SIZE", str(self.max_threads * 4)))
            except Exception:
                mq = self.max_threads * 4
            max_queue_size = max(1, mq)

        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self.is_running = False
        self.active_tasks: set = set()

        # ОДИН SOCKS-порт (по умолчанию 9050).
        try:
            self.first_socks_port = int(os.getenv("TOR_SOCKS_PORT", "9050"))
        except Exception:
            self.first_socks_port = 9050
        self.ports = [self.first_socks_port]

        self.circuits_video = int(os.getenv("TOR_CIRCUITS_VIDEO", "6"))
        self.circuits_audio = int(os.getenv("TOR_CIRCUITS_AUDIO", "4"))  # ⬆️
        self.circuits_default = int(os.getenv("TOR_CIRCUITS_DEFAULT", "4"))

        self.DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")
        self.tor_dl_override = os.getenv("TOR_DL_BIN", "").strip() or None
        self._flags_map: Optional[Dict[str, Optional[str]]] = None

        self.fallback_enabled = os.getenv("YT_FALLBACK", "1").strip() != "0"

        # Фолбэк-политика
        fb_tor_env = (os.getenv("YT_FALLBACK_TOR", "auto").strip().lower() or "auto")
        self.fallback_tor_mode = fb_tor_env  # "auto" | "1" | "0"
        self.audio_prefer_curl = os.getenv("YT_AUDIO_PREFER_CURL", "1").strip() != "0"

        # ВСЕГДА последовательно (видео → аудио)
        self.parallel_av_enabled = False

    def _ensure_download_dir(self):
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        safe_log(f"📂 Папка для загрузки: {self.DOWNLOAD_DIR}")

    def _selfcheck_binaries(self):
        if shutil.which("ffmpeg") is None:
            safe_log("⚠️ ffmpeg не найден в PATH — перекодирование/слияние может не работать.")
        if shutil.which("ffprobe") is None:
            safe_log("⚠️ ffprobe не найден в PATH — валидатор контейнера ограничен.")

    @cached_property
    def user_agent(self):
        ua = os.getenv("TOR_DL_UA", "").strip()
        if ua:
            return ua
        try:
            return UserAgent().random
        except Exception:
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    def referer(self) -> str:
        return os.getenv("TOR_DL_REFERER", "https://www.youtube.com/")

    # ---------- Public API ---------- #

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

    # ---------- Queue internals ---------- #

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
                safe_log(_fmt_info("🎯 AUDIO pick", a_fmt))
                names = _make_names(info, None, a_fmt, target_h)
                audio_path = os.path.join(self.DOWNLOAD_DIR, names["audio_fn"])

                if a_fmt:
                    await self._download_with_tordl_then_fallback(
                        a_fmt["url"], audio_path, "audio", expected_size=_expected_size(a_fmt)
                    )
                    result = audio_path
                else:
                    prog = _pick_best_progressive(fmts, target_h)
                    safe_log(_fmt_info("🎯 PROGRESSIVE (for audio)", prog))
                    if not prog:
                        raise Exception("❌ Не найден подходящий аудио/прогрессивный поток (direct).")
                    prog_ext = _fmt_ext(prog) or "mp4"
                    prog_tmp = os.path.join(self.DOWNLOAD_DIR, f"{uuid.uuid4()}.prog.{prog_ext}")
                    await self._download_with_tordl_then_fallback(
                        prog["url"], prog_tmp, "video", expected_size=_expected_size(prog)
                    )
                    await self._extract_audio_from_file(prog_tmp, audio_path)
                    result = audio_path
                    try:
                        os.remove(prog_tmp)
                    except Exception:
                        pass

            else:
                cand_itags = VIDEO_ITAG_CANDIDATES.get(target_h) or []
                v_fmt = _pick_by_itag_list(fmts, cand_itags) or _pick_best_video_by_height(fmts, target_h)
                a_fmt = _pick_best_audio(fmts)
                safe_log(f"🎯 Requested: {target_h}p")
                safe_log(_fmt_info("🎯 VIDEO pick", v_fmt))
                safe_log(_fmt_info("🎯 AUDIO pick", a_fmt))

                if v_fmt and a_fmt:
                    names = _make_names(info, v_fmt, a_fmt, target_h)
                    v_path = os.path.join(self.DOWNLOAD_DIR, names["video_fn"])
                    a_path = os.path.join(self.DOWNLOAD_DIR, names["audio_fn"])

                    # СТРОГО ПО ОЧЕРЕДИ: ВИДЕО → АУДИО
                    await self._download_with_tordl_then_fallback(
                        v_fmt["url"], v_path, "video", expected_size=_expected_size(v_fmt)
                    )
                    await self._download_with_tordl_then_fallback(
                        a_fmt["url"], a_path, "audio", expected_size=_expected_size(a_fmt)
                    )

                    # merge
                    out = await self._merge_files(
                        {"video": v_path, "audio": a_path, "output_base": os.path.join(self.DOWNLOAD_DIR, names["merge_base"])},
                        v_fmt, a_fmt
                    )
                    result = out
                else:
                    prog = _pick_best_progressive(fmts, target_h)
                    safe_log(_fmt_info("🎯 PROGRESSIVE pick", prog))
                    if not prog:
                        missing = []
                        if not v_fmt: missing.append("видео")
                        if not a_fmt: missing.append("аудио")
                        raise Exception(f"❌ Не найден direct поток: {', '.join(missing)}; и нет прогрессивного.")
                    names = _make_names(info, prog, None, target_h)
                    prog_path = os.path.join(self.DOWNLOAD_DIR, names["prog_fn"])
                    await self._download_with_tordl_then_fallback(
                        prog["url"], prog_path, "video", expected_size=_expected_size(prog)
                    )
                    result = prog_path

            return result

        finally:
            try:
                size = os.path.getsize(result) if result and os.path.exists(result) else 0
                duration = time.time() - start_proc
                avg = size / duration if duration > 0 else 0
                safe_log(f"📈 Process: {download_type.upper()} {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
            except Exception:
                pass

    # ---------- tor-dl path & dynamic flags ---------- #

    def _resolve_tor_dl_path(self) -> str:
        exe_name = "tor-dl.exe" if platform.system() == "Windows" else "tor-dl"
        # override
        if self.tor_dl_override:
            p = os.path.abspath(self.tor_dl_override)
            if os.path.isfile(p):
                return p
        # PATH
        found = shutil.which(exe_name)
        if found and os.path.isfile(found):
            return os.path.abspath(found)
        # common places
        candidates = [
            os.path.join(".", exe_name),
            os.path.join("/app", exe_name),
            os.path.join("/usr/local/bin", exe_name),
            os.path.join("/usr/bin", exe_name),
        ]
        for p in candidates:
            if os.path.isfile(p):
                return os.path.abspath(p)
        raise FileNotFoundError("tor-dl не найден. Задай ENV TOR_DL_BIN или положи бинарь в PATH.")

    def _load_flags_map(self, executable_abs: str) -> Dict[str, Optional[str]]:
        if self._flags_map is not None:
            return self._flags_map
        try:
            proc = subprocess.run([executable_abs, "-h"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            help_txt = (proc.stdout + proc.stderr).decode("utf-8", "ignore")
        except Exception as e:
            help_txt = ""
            safe_log(f"⚠️ Не удалось получить -h от tor-dl: {e}. Включу только базовые флаги.")

        def has(flag: str) -> bool:
            return re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|,|$)", help_txt) is not None

        m: Dict[str, Optional[str]] = {}
        m["ports"]     = "-ports" if has("-ports") else None
        m["circuits"]  = "-circuits" if has("-circuits") else ("-c" if has("-c") else None)
        m["name"]      = "-n" if has("-n") else ("-name" if has("-name") else None)
        m["dest"]      = "-d" if has("-d") else ("-destination" if has("-destination") else None)
        m["force"]     = "-force" if has("-force") else None

        m["rps"]            = "-rps" if has("-rps") else None
        m["segment_size"]   = "-segment-size" if has("-segment-size") else None
        m["max_retries"]    = "-max-retries" if has("-max-retries") else None
        m["min_lifetime"]   = "-min-lifetime" if has("-min-lifetime") else ("-l" if has("-l") else None)
        m["retry_base_ms"]  = "-retry-base-ms" if has("-retry-base-ms") else None
        m["tail_threshold"] = "-tail-threshold" if has("-tail-threshold") else None
        m["tail_workers"]   = "-tail-workers" if has("-tail-workers") else None
        m["tail_shard_min"] = "-tail-shard-min" if has("-tail-shard-min") else None
        m["tail_shard_max"] = "-tail-shard-max" if has("-tail-shard-max") else None
        m["allow_http"]     = "-allow-http" if has("-allow-http") else None
        m["user_agent"]     = "-user-agent" if has("-user-agent") else None
        m["referer"]        = "-referer" if has("-referer") else None
        m["silent"]         = "-silent" if has("-silent") else None
        m["verbose"]        = "-verbose" if has("-verbose") else ("-v" if has("-v") else None)
        m["quiet"]          = "-quiet" if has("-quiet") else ("-q" if has("-q") else None)

        picked = {k: v for k, v in m.items() if v}
        if picked:
            safe_log("🧭 tor-dl флаги (доступны): " + ", ".join(f"{k}={v}" for k, v in picked.items()))
        else:
            safe_log("🧭 tor-dl: нет распознанных флагов (-h не дал сигнатуры)")
        self._flags_map = m
        return m

    def _build_common_flags(self, flags: Dict[str, Optional[str]]) -> List[str]:
        out: List[str] = []
        # perf / behavior
        if os.getenv("TOR_DL_RPS") and flags.get("rps"):
            out += [flags["rps"], os.getenv("TOR_DL_RPS")]
        if os.getenv("TOR_DL_TAIL_THRESHOLD") and flags.get("tail_threshold"):
            out += [flags["tail_threshold"], os.getenv("TOR_DL_TAIL_THRESHOLD")]
        if os.getenv("TOR_DL_TAIL_WORKERS") and flags.get("tail_workers"):
            out += [flags["tail_workers"], os.getenv("TOR_DL_TAIL_WORKERS")]
        if os.getenv("TOR_DL_SEGMENT_SIZE") and flags.get("segment_size"):
            out += [flags["segment_size"], os.getenv("TOR_DL_SEGMENT_SIZE")]
        if os.getenv("TOR_DL_SEGMENT_RETRIES") and flags.get("max_retries"):
            out += [flags["max_retries"], os.getenv("TOR_DL_SEGMENT_RETRIES")]
        if os.getenv("TOR_DL_MIN_LIFETIME") and flags.get("min_lifetime"):
            out += [flags["min_lifetime"], os.getenv("TOR_DL_MIN_LIFETIME")]
        if os.getenv("TOR_DL_RETRY_BASE_MS") and flags.get("retry_base_ms"):
            out += [flags["retry_base_ms"], os.getenv("TOR_DL_RETRY_BASE_MS")]
        if os.getenv("TOR_DL_TAIL_SHARD_MIN") and flags.get("tail_shard_min"):
            out += [flags["tail_shard_min"], os.getenv("TOR_DL_TAIL_SHARD_MIN")]
        if os.getenv("TOR_DL_TAIL_SHARD_MAX") and flags.get("tail_shard_max"):
            out += [flags["tail_shard_max"], os.getenv("TOR_DL_TAIL_SHARD_MAX")]

        # verbosity
        if os.getenv("TOR_DL_SILENT", "").strip() == "1" and flags.get("silent"):
            out += [flags["silent"]]
        else:
            if os.getenv("TOR_DL_VERBOSE", "").strip() == "1" and flags.get("verbose"):
                out += [flags["verbose"]]
            elif os.getenv("TOR_DL_QUIET", "").strip() == "1" and flags.get("quiet"):
                out += [flags["quiet"]]

        # safety / headers
        if os.getenv("TOR_DL_ALLOW_HTTP", "").strip() == "1" and flags.get("allow_http"):
            out += [flags["allow_http"]]
        if self.user_agent and flags.get("user_agent"):
            out += [flags["user_agent"], self.user_agent]
        if self.referer() and flags.get("referer"):
            out += [flags["referer"], self.referer()]

        return out

    def _build_cmd(
        self,
        executable_abs: str,
        flags: Dict[str, Optional[str]],
        circuits_val: int,
        tor_name: str,
        dest_dir: str,
        url: str,
    ) -> List[str]:
        cmd: List[str] = [executable_abs]

        # один порт — CSV из одного значения
        if flags.get("ports"):
            cmd += [flags["ports"], str(self.first_socks_port)]

        if flags.get("circuits"):
            cmd += [flags["circuits"], str(circuits_val)]

        # имя файла и директория — добавляем ТОЛЬКО если поддерживает
        if flags.get("name"):
            cmd += [flags["name"], tor_name]
        if flags.get("dest"):
            cmd += [flags["dest"], dest_dir]

        # overwrite
        if flags.get("force"):
            cmd += [flags["force"]]

        cmd += self._build_common_flags(flags)
        cmd += [url]
        return cmd

    # ---------- Streaming process runner ---------- #

    async def _run_and_stream(self, cmd: List[str], cwd: Optional[str] = None, env: Optional[dict] = None) -> Tuple[int, str]:
        """
        Запускает процесс и ПОТОКОВО печатает stdout/stderr в реальном времени.
        Возвращает (rc, combined_tail_text).
        """
        safe_log("🧪 CMD:\n" + _join_cmd_for_log(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        tail_lines: List[str] = []

        async def _pump(stream: asyncio.StreamReader, prefix: str):
            nonlocal tail_lines
            buf = b""
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                buf += chunk
                # разбираем по строкам
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    txt = line.decode("utf-8", "ignore").rstrip("\r")
                    if txt:
                        safe_log(f"{prefix} {txt}")
                        tail_lines.append(txt)
                        if len(tail_lines) > 200:
                            tail_lines = tail_lines[-200:]
            # остаток без \n
            if buf:
                txt = buf.decode("utf-8", "ignore")
                if txt:
                    safe_log(f"{prefix} {txt}")
                    tail_lines.append(txt)
                    if len(tail_lines) > 200:
                        tail_lines = tail_lines[-200:]

        await asyncio.gather(
            _pump(proc.stdout, "▶"),
            _pump(proc.stderr, "⚠"),
        )

        rc = await proc.wait()
        combined_tail = "\n".join(tail_lines)
        safe_log(f"🏁 RC={rc}")
        return rc, combined_tail

    # ---------- tor-dl + smart fallback ---------- #

    async def _download_with_tordl_then_fallback(self, url: str, filename: str, media_type: str, expected_size: int = 0) -> str:
        """Порядок:
        1) если audio и разрешено — пробуем curl/wget (по политике TOR: auto/1/0)
        2) tor-dl (всегда),
        3) если не вышло — ещё раз curl/wget, но уже через TOR (если 403/auto).
        """
        # 1) Быстрый фолбэк для аудио
        prefer_curl = (media_type == "audio" and self.audio_prefer_curl and self.fallback_enabled)
        if prefer_curl:
            use_tor = self._decide_fallback_tor(url)
            ok, rc, tail = await self._download_with_curl_or_wget(url, filename, media_type, expected_size, use_tor)
            if ok:
                return filename
            # если был 403 и мы шли БЕЗ TOR — мгновенный повтор через TOR
            if (not use_tor) and self._looks_like_403(rc, tail):
                safe_log("🟠 curl/wget дал 403 без TOR — повторю через TOR")
                ok, _, _ = await self._download_with_curl_or_wget(url, filename, media_type, expected_size, True)
                if ok:
                    return filename

        # 2) tor-dl
        ok = await self._download_with_tordl(url, filename, media_type, expected_size)
        if ok:
            return filename

        # 3) последняя попытка через curl/wget (с принудительным TOR, если auto/403)
        if self.fallback_enabled:
            use_tor = True if self.fallback_tor_mode in ("1", "true", "yes") else self._decide_fallback_tor(url)
            ok2, rc2, tail2 = await self._download_with_curl_or_wget(url, filename, media_type, expected_size, use_tor)
            if ok2:
                return filename
            # если вдруг последние попытки тоже безуспешны, пробуем зеркально (TOR<->noTOR)
            if self.fallback_tor_mode == "auto":
                ok3, _, _ = await self._download_with_curl_or_wget(url, filename, media_type, expected_size, not use_tor)
                if ok3:
                    return filename

        raise Exception(f"Не удалось загрузить {media_type}: tor-dl и curl/wget провалились")

    def _decide_fallback_tor(self, url: str) -> bool:
        mode = self.fallback_tor_mode
        if mode in ("1", "true", "yes"):
            return True
        if mode in ("0", "false", "no"):
            # auto-переключение при явной привязке к IP
            return _url_requires_tor(url)
        # auto (по умолчанию)
        return _url_requires_tor(url)

    def _looks_like_403(self, rc: int, tail: str) -> bool:
        t = (tail or "").lower()
        return rc in (22, 8) or "403" in t or "forbidden" in t

    async def _download_with_tordl(self, url: str, filename: str, media_type: str, expected_size: int) -> bool:
        attempts = 0
        max_attempts = 2  # tor-dl сам ретраит/шардит — снаружи много не надо
        circuits_val = self._pick_circuits(url, media_type)
        dest_dir = os.path.dirname(os.path.abspath(filename)) or "."
        base_name = os.path.basename(filename)

        while attempts < max_attempts:
            attempts += 1
            try:
                executable_abs = os.path.abspath(self._resolve_tor_dl_path())
                try:
                    if not os.access(executable_abs, os.X_OK):
                        os.chmod(executable_abs, os.stat(executable_abs).st_mode | stat.S_IEXEC)
                except Exception:
                    pass

                flags = self._load_flags_map(executable_abs)
                try:
                    if os.path.exists(filename):
                        os.remove(filename)
                except Exception:
                    pass

                cmd = self._build_cmd(executable_abs, flags, circuits_val, base_name, dest_dir, url)
                start = time.time()
                rc, tail = await self._run_and_stream(cmd, cwd=dest_dir)

                # Если -n/-d недоступны у tor-dl, он мог сохранить как "videoplayback*".
                if rc == 0 and (not os.path.exists(filename)):
                    self._try_fix_output_name(dest_dir, filename, start)

                if rc == 0 and os.path.exists(filename) and os.path.getsize(filename) > 0:
                    if self._is_download_complete(filename, media_type, expected_size):
                        size = os.path.getsize(filename)
                        dur = time.time() - start
                        spd = size / dur if dur > 0 else 0
                        safe_log(f"✅ {media_type.upper()} via tor-dl: {size/1024/1024:.1f}MB за {dur:.1f}s ({spd/1024/1024:.1f} MB/s)")
                        return True

                if "Failed to retrieve content length" in (tail or ""):
                    safe_log("🟠 tor-dl не получил Content-Length — перехожу к фолбэку.")
                    return False

            except FileNotFoundError as e:
                safe_log(f"❌ tor-dl не найден: {e}")
                return False
            except Exception as e:
                safe_log(f"❌ Ошибка запуска tor-dl: {e}")

            await asyncio.sleep(0.5)

        return False

    def _try_fix_output_name(self, dest_dir: str, expected_path: str, start_ts: float):
        """
        Если tor-dl не поддержал -n/-d и сохранил файл как 'videoplayback*',
        пробуем найти свежий файл и переименовать в ожидаемое имя.
        """
        try:
            cand = None
            for fn in os.listdir(dest_dir):
                low = fn.lower()
                if not low.startswith("videoplayback"):
                    continue
                full = os.path.join(dest_dir, fn)
                if not os.path.isfile(full):
                    continue
                st = os.stat(full)
                # файл должен быть создан "сейчас"
                if st.st_mtime >= (start_ts - 60) and st.st_size > 0:
                    cand = full
                    break
            if cand and (not os.path.exists(expected_path)):
                os.replace(cand, expected_path)
                safe_log(f"📝 Переименован вывод tor-dl: {os.path.basename(cand)} → {os.path.basename(expected_path)}")
        except Exception:
            pass

    async def _download_with_curl_or_wget(self, url: str, filename: str, media_type: str, expected_size: int, use_tor: bool) -> Tuple[bool, int, str]:
        ua = self.user_agent
        ref = self.referer()
        socks_host = "127.0.0.1"
        socks_port = str(self.first_socks_port)

        # curl
        curl = shutil.which("curl")
        if curl:
            cmd = [
                curl, "-L", "--fail", "--retry", "3", "--retry-delay", "1",
                "--connect-timeout", "20", "--max-time", "1800",
                "-A", ua, "-e", ref,
            ]
            if use_tor:
                cmd += ["--socks5-hostname", f"{socks_host}:{socks_port}"]
            cmd += ["-o", filename, url]
            start = time.time()
            rc, tail = await self._run_and_stream(cmd)
            if rc == 0 and os.path.exists(filename) and os.path.getsize(filename) > 0 and self._is_download_complete(filename, media_type, expected_size):
                size = os.path.getsize(filename)
                dur = time.time() - start
                safe_log(f"✅ {media_type.upper()} via curl{'(TOR)' if use_tor else ''}: {size/1024/1024:.1f}MB за {dur:.1f}s")
                return True, rc, tail
            if self._looks_like_403(rc, tail):
                return False, rc, tail

        # wget
        wget = shutil.which("wget")
        if wget:
            env = os.environ.copy()
            if use_tor:
                env["https_proxy"] = f"socks5h://{socks_host}:{socks_port}"
                env["http_proxy"]  = f"socks5h://{socks_host}:{socks_port}"
            else:
                env.pop("https_proxy", None)
                env.pop("http_proxy", None)
            cmd = [
                wget, "-O", filename, "--tries=3", "--timeout=20",
                f"--user-agent={ua}", f"--referer={ref}", url
            ]
            start = time.time()
            rc, tail = await self._run_and_stream(cmd, env=env)
            if rc == 0 and os.path.exists(filename) and os.path.getsize(filename) > 0 and self._is_download_complete(filename, media_type, expected_size):
                size = os.path.getsize(filename)
                dur = time.time() - start
                safe_log(f"✅ {media_type.upper()} via wget{'(TOR)' if use_tor else ''}: {size/1024/1024:.1f}MB за {dur:.1f}s")
                return True, rc, tail
            if self._looks_like_403(rc, tail):
                return False, rc, tail

        safe_log("❌ Нет curl/wget или они тоже не справились.")
        return False, rc if 'rc' in locals() else -1, tail if 'tail' in locals() else ""

    def _pick_circuits(self, url: str, media_type: str) -> int:
        h = (url or "").lower()
        if "googlevideo" in h or "youtube" in h:
            return self.circuits_audio if media_type == "audio" else self.circuits_video
        return self.circuits_default

    def _is_download_complete(self, filename: str, media_type: str, expected_size: int = 0) -> bool:
        try:
            size = os.path.getsize(filename)
            min_audio_size = 256 * 1024
            min_video_size = 2 * 1024 * 1024
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

    # ---------- Post-processing helpers ---------- #

    async def _extract_audio_from_file(self, input_path: str, output_audio_path: str):
        cmd_copy = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-vn", "-c:a", "copy",
            "-y", output_audio_path
        ]
        rc = subprocess.call(cmd_copy, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc == 0 and os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 0:
            safe_log(f"🎧 Audio extracted (copy): {output_audio_path}")
            return

        cmd_trans = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-vn", "-c:a", "aac", "-b:a", "192k",
            "-y", output_audio_path
        ]
        rc2 = subprocess.call(cmd_trans, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if rc2 != 0 or not os.path.exists(output_audio_path) or os.path.getsize(output_audio_path) == 0:
            raise Exception("❌ Не удалось извлечь аудио из прогрессивного файла.")
        safe_log(f"🎧 Audio extracted (aac): {output_audio_path}")

    # ---------- Merging video+audio ---------- #

    async def _merge_files(self, paths: Dict[str, str], v_fmt: Optional[dict] = None, a_fmt: Optional[dict] = None) -> str:
        vcodec = _fmt_vc(v_fmt or {})
        acodec = _fmt_ac(a_fmt or {})
        video_path = paths["video"]
        audio_path = paths["audio"]

        def run_ffmpeg(cmd: List[str]):
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return proc.returncode, proc.stdout, proc.stderr

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
