#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
YtDlpDownloader (v2.2)
— Под НОВЫЙ tor-dl, один SOCKS порт (9050), потоковые логи,
— раздельные имена файлов, имя передается прямо в tor-dl через -n/-name,
— ближайшее качество по высоте, fallback на curl/wget через Tor.

Внешние контракты сохранены:
  class YtDlpDownloader (singleton)
  methods: start_workers(), stop(), download(url, download_type="video", quality="480", progress_msg=None)

ENV (опционально):
  YT_MAX_THREADS, YT_QUEUE_SIZE
  TOR_DL_BIN                 — путь к tor-dl (если не в PATH)
  TOR_SOCKS_PORT            — одиночный SOCKS-порт (default 9050)
  TOR_CIRCUITS_VIDEO         — default 6
  TOR_CIRCUITS_AUDIO         — default 1
  TOR_CIRCUITS_DEFAULT       — default 4
  TOR_DL_RPS, TOR_DL_SEGMENT_SIZE, TOR_DL_SEGMENT_RETRIES, TOR_DL_MIN_LIFETIME,
  TOR_DL_RETRY_BASE_MS, TOR_DL_TAIL_THRESHOLD, TOR_DL_TAIL_WORKERS,
  TOR_DL_TAIL_SHARD_MIN, TOR_DL_TAIL_SHARD_MAX,
  TOR_DL_ALLOW_HTTP (1/0), TOR_DL_VERBOSE (1/0), TOR_DL_QUIET (1/0), TOR_DL_SILENT (1/0),
  TOR_DL_UA, TOR_DL_REFERER (default https://www.youtube.com/)
  DOWNLOAD_DIR               — куда кидать файлы (default /downloads)
  YT_FALLBACK                — 1 (включено) / 0 (выключить curl/wget)
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
import string
from functools import cached_property
from typing import Dict, List, Optional, Tuple

from fake_useragent import UserAgent
from tqdm import tqdm

# ваши утилиты (контракт сохранён)
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


# -------------------- Name helpers -------------------- #

_WIN_FORBIDDEN = '<>:"/\\|?*'
_CTRL = "".join(map(chr, range(0, 32)))
_FORBIDDEN = set(_CTRL + "\x7f")

def _sanitize_name(title: str, allow_spaces: bool = True) -> str:
    """
    Безопасное имя: сохраняем пробелы (по запросу), выпиливаем опасные символы,
    схлопываем множественные пробелы, обрезаем края.
    """
    if not title:
        title = "download"
    s = title.strip()
    # заменить непечатаемые и слеши на пробел
    repl = []
    for ch in s:
        if ch in _FORBIDDEN:
            repl.append(" ")
        elif ch in _WIN_FORBIDDEN:
            repl.append(" ")
        elif ch in "/":
            repl.append(" ")
        else:
            repl.append(ch)
    s = "".join(repl)
    # схлопнуть пробелы
    s = re.sub(r"\s+", " ", s)
    if not allow_spaces:
        s = s.replace(" ", "_")
    # ограничить длину
    return s[:180].strip() or "download"

def _ensure_parent_dir(path: str):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)

def _unique_path(dirpath: str, filename: str) -> str:
    """
    Если такой файл уже есть — добавляем суффикс ' (1)', ' (2)', ...
    """
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dirpath, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dirpath, f"{base} ({i}){ext}")
        i += 1
    return candidate

def _mk_name_from_info(info: dict, kind: str, ext: str, height: Optional[int] = None) -> str:
    """
    kind: 'video', 'audio', 'prog', 'merge'
    """
    title = (info.get("title") or "download").strip()
    title = _sanitize_name(title, allow_spaces=True)
    if kind == "video":
        tag = f"[V{height or ''}p]".replace("p]", "]")
        return f"{title} {tag}.{ext}"
    if kind == "audio":
        return f"{title} [AUDIO].{ext}"
    if kind == "prog":
        tag = f"[PROG{height or ''}p]".replace("p]", "]")
        return f"{title} {tag}.{ext}"
    if kind == "merge":
        tag = f"[{height or ''}p]".replace("p]", "]")
        return f"{title} {tag}.{ext}"
    return f"{title}.{ext}"


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
    """Абсолютная близость к target_h; при равенстве — предпочтение формату из PROGRESSIVE_PREFERENCE и бОльшему TBR."""
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

        # Один SOCKS порт
        try:
            self.first_socks_port = int(os.getenv("TOR_SOCKS_PORT", "9050"))
        except Exception:
            self.first_socks_port = 9050
        self.ports = [self.first_socks_port]

        self.circuits_video = int(os.getenv("TOR_CIRCUITS_VIDEO", "6"))
        self.circuits_audio = int(os.getenv("TOR_CIRCUITS_AUDIO", "1"))
        self.circuits_default = int(os.getenv("TOR_CIRCUITS_DEFAULT", "4"))

        self.DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")
        self.tor_dl_override = os.getenv("TOR_DL_BIN", "").strip() or None
        self._flags_map: Optional[Dict[str, Optional[str]]] = None

        self.fallback_enabled = os.getenv("YT_FALLBACK", "1").strip() != "0"

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
        cleanup_targets: List[str] = []
        result: Optional[str] = None
        try:
            try:
                target_h = int(quality)
            except Exception:
                target_h = 480

            info = await get_video_info_with_cache(url)
            fmts = _formats_from_info(info)

            # ---- AUDIO ONLY ----
            if download_type == "audio":
                a_fmt = _pick_best_audio(fmts)
                safe_log(_fmt_info("🎯 AUDIO pick", a_fmt))
                if a_fmt:
                    aext = _fmt_ext(a_fmt) or ("m4a" if "aac" in _fmt_ac(a_fmt) else "webm")
                    a_name = _mk_name_from_info(info, "audio", aext)
                    a_path = _unique_path(self.DOWNLOAD_DIR, a_name)
                    _ensure_parent_dir(a_path)
                    await self._download_with_tordl_then_fallback(
                        a_fmt["url"], a_path, "audio", expected_size=_expected_size(a_fmt)
                    )
                    result = a_path
                else:
                    prog = _pick_best_progressive(fmts, target_h)
                    safe_log(_fmt_info("🎯 PROGRESSIVE (for audio)", prog))
                    if not prog:
                        raise Exception("❌ Не найден подходящий аудио/прогрессивный поток (direct).")
                    pext = _fmt_ext(prog) or "mp4"
                    p_name = _mk_name_from_info(info, "prog", pext, height=_fmt_height(prog))
                    prog_path = _unique_path(self.DOWNLOAD_DIR, p_name)
                    _ensure_parent_dir(prog_path)
                    await self._download_with_tordl_then_fallback(
                        prog["url"], prog_path, "video", expected_size=_expected_size(prog)
                    )
                    out_audio = _unique_path(self.DOWNLOAD_DIR, _mk_name_from_info(info, "audio", "m4a"))
                    await self._extract_audio_from_file(prog_path, out_audio)
                    result = out_audio
                    cleanup_targets.append(prog_path)

                return result

            # ---- VIDEO (+AUDIO) ----
            cand_itags = VIDEO_ITAG_CANDIDATES.get(target_h) or []
            v_fmt = _pick_by_itag_list(fmts, cand_itags) or _pick_best_video_by_height(fmts, target_h)
            a_fmt = _pick_best_audio(fmts)
            safe_log(f"🎯 Requested: {target_h}p")
            safe_log(_fmt_info("🎯 VIDEO pick", v_fmt))
            safe_log(_fmt_info("🎯 AUDIO pick", a_fmt))

            if v_fmt and a_fmt:
                vext = _fmt_ext(v_fmt) or ("mp4" if ("avc" in _fmt_vc(v_fmt) or "h264" in _fmt_vc(v_fmt)) else "webm")
                aext = _fmt_ext(a_fmt) or ("m4a" if "aac" in _fmt_ac(a_fmt) else "webm")

                # разные имена для tor-dl
                v_name = _mk_name_from_info(info, "video", vext, height=_fmt_height(v_fmt))
                a_name = _mk_name_from_info(info, "audio", aext)
                v_path = _unique_path(self.DOWNLOAD_DIR, v_name)
                a_path = _unique_path(self.DOWNLOAD_DIR, a_name)
                _ensure_parent_dir(v_path); _ensure_parent_dir(a_path)

                await asyncio.gather(
                    self._download_with_tordl_then_fallback(v_fmt["url"], v_path, "video", expected_size=_expected_size(v_fmt)),
                    self._download_with_tordl_then_fallback(a_fmt["url"], a_path, "audio", expected_size=_expected_size(a_fmt)),
                )
                cleanup_targets += [v_path, a_path]

                # merge → красивое имя
                out_name = _mk_name_from_info(info, "merge", "mp4", height=_fmt_height(v_fmt) or target_h)
                output_base = os.path.splitext(_unique_path(self.DOWNLOAD_DIR, out_name))[0]
                result = await self._merge_files(
                    {"video": v_path, "audio": a_path, "output_base": output_base},
                    v_fmt, a_fmt
                )
            else:
                prog = _pick_best_progressive(fmts, target_h)
                safe_log(_fmt_info("🎯 PROGRESSIVE pick", prog))
                if not prog:
                    missing = []
                    if not v_fmt: missing.append("видео")
                    if not a_fmt: missing.append("аудио")
                    raise Exception(f"❌ Не найден direct поток: {', '.join(missing)}; и нет прогрессивного.")
                pext = _fmt_ext(prog) or "mp4"
                p_name = _mk_name_from_info(info, "prog", pext, height=_fmt_height(prog))
                prog_path = _unique_path(self.DOWNLOAD_DIR, p_name)
                _ensure_parent_dir(prog_path)
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
                safe_log(f"📈 Process: {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
            except Exception:
                pass
            # очистка временных
            for p in cleanup_targets:
                try:
                    if p and os.path.exists(p) and (not result or os.path.abspath(p) != os.path.abspath(result)):
                        os.remove(p)
                        safe_log(f"🧹 Удален временный файл: {p}")
                except Exception:
                    pass

    # ---------- tor-dl path & flags ---------- #

    def _resolve_tor_dl_path(self) -> str:
        exe_name = "tor-dl.exe" if platform.system() == "Windows" else "tor-dl"
        if self.tor_dl_override:
            p = os.path.abspath(self.tor_dl_override)
            if os.path.isfile(p):
                return p
        found = shutil.which(exe_name)
        if found and os.path.isfile(found):
            return os.path.abspath(found)
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
        safe_log("🧭 tor-dl флаги (доступны): " + (", ".join(f"{k}={v}" for k, v in picked.items()) if picked else "нет распознанных флагов"))
        self._flags_map = m
        return m

    def _build_common_flags(self, flags: Dict[str, Optional[str]]) -> List[str]:
        out: List[str] = []
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

        if os.getenv("TOR_DL_SILENT", "").strip() == "1" and flags.get("silent"):
            out += [flags["silent"]]
        else:
            if os.getenv("TOR_DL_VERBOSE", "").strip() == "1" and flags.get("verbose"):
                out += [flags["verbose"]]
            elif os.getenv("TOR_DL_QUIET", "").strip() == "1" and flags.get("quiet"):
                out += [flags["quiet"]]

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

        if flags.get("ports"):
            cmd += [flags["ports"], str(self.first_socks_port)]
        if flags.get("circuits"):
            cmd += [flags["circuits"], str(circuits_val)]
        if flags.get("name"):
            # ВАЖНО: передаём имя с пробелами — subprocess передаёт аргументы без shell, всё ок
            cmd += [flags["name"], tor_name]
        if flags.get("dest"):
            cmd += [flags["dest"], dest_dir]
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
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    txt = line.decode("utf-8", "ignore").rstrip("\r")
                    if txt:
                        safe_log(f"{prefix} {txt}")
                        tail_lines.append(txt)
                        if len(tail_lines) > 200:
                            tail_lines = tail_lines[-200:]
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

    # ---------- tor-dl + optional fallback ---------- #

    async def _download_with_tordl_then_fallback(self, url: str, filename: str, media_type: str, expected_size: int = 0) -> str:
        ok = await self._download_with_tordl(url, filename, media_type, expected_size)
        if ok:
            return filename
        if self.fallback_enabled:
            ok2 = await self._download_with_curl_or_wget(url, filename, media_type, expected_size)
            if ok2:
                return filename
        raise Exception(f"Не удалось загрузить {media_type}: tor-dl{' и curl/wget' if self.fallback_enabled else ''} провалились")

    async def _download_with_tordl(self, url: str, filename: str, media_type: str, expected_size: int) -> bool:
        attempts = 0
        max_attempts = 2  # tor-dl сам ретраит/шардит — снаружи много не нужно
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

    async def _download_with_curl_or_wget(self, url: str, filename: str, media_type: str, expected_size: int) -> bool:
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
                "--socks5-hostname", f"{socks_host}:{socks_port}",
                "-o", filename, url
            ]
            _ensure_parent_dir(filename)
            start = time.time()
            rc, _ = await self._run_and_stream(cmd)
            if rc == 0 and os.path.exists(filename) and os.path.getsize(filename) > 0 and self._is_download_complete(filename, media_type, expected_size):
                size = os.path.getsize(filename)
                dur = time.time() - start
                safe_log(f"✅ {media_type.upper()} via curl: {size/1024/1024:.1f}MB за {dur:.1f}s")
                return True

        # wget
        wget = shutil.which("wget")
        if wget:
            env = os.environ.copy()
            env["https_proxy"] = f"socks5h://{socks_host}:{socks_port}"
            env["http_proxy"]  = f"socks5h://{socks_host}:{socks_port}"
            cmd = [
                wget, "-O", filename, "--tries=3", "--timeout=20",
                f"--user-agent={ua}", f"--referer={ref}", url
            ]
            _ensure_parent_dir(filename)
            start = time.time()
            rc, _ = await self._run_and_stream(cmd, env=env)
            if rc == 0 and os.path.exists(filename) and os.path.getsize(filename) > 0 and self._is_download_complete(filename, media_type, expected_size):
                size = os.path.getsize(filename)
                dur = time.time() - start
                safe_log(f"✅ {media_type.upper()} via wget: {size/1024/1024:.1f}MB за {dur:.1f}s")
                return True

        safe_log("❌ Нет curl/wget или они тоже не справились.")
        return False

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
