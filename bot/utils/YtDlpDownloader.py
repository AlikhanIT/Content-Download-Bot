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

import aiofiles
from fake_useragent import UserAgent
from tqdm import tqdm

from bot.utils.video_info import get_video_info_with_cache
from bot.utils.log import log_action


def safe_log(msg: str):
    tqdm.write(msg)
    log_action(msg)


# -------------------- Формат-помощники -------------------- #

# Кандидаты для каждой целевой высоты (сначала h264/mp4, затем vp9/webm, затем av1)
VIDEO_ITAG_CANDIDATES = {
    2160: ["266", "401", "315", "272"],  # h264, av1/vp9 варианты
    1440: ["264", "400", "308", "271"],
    1080: ["137", "399", "248", "614", "616", "270"],
    720:  ["136", "398", "247", "232", "609"],
    480:  ["135", "397", "244", "231", "606"],
    360:  ["134", "396", "243", "230", "605", "18"],
    240:  ["133", "395", "242", "229", "604"],
    144:  ["160", "394", "278", "269", "603"],
}

AUDIO_ITAG_PREFERRED = ["140", "141", "139", "251", "250", "249"]  # m4a приоритетнее

# Протоколы, которые умеем качать напрямую (без HLS/DASH)
DIRECT_PROTOCOLS = {"https", "http"}


def _formats_from_info(info: dict):
    fmts = info.get("formats") or []
    # yt-dlp иногда кладёт отобранные форматы в "requested_formats"
    requested = info.get("requested_formats") or []
    if requested:
        # requested_formats — список из (bestvideo, bestaudio)
        for rf in requested:
            if rf and isinstance(rf, dict):
                fmts.append(rf)
    return fmts


def _is_direct(fmt: dict) -> bool:
    proto = (fmt.get("protocol") or "").lower()
    return (proto in DIRECT_PROTOCOLS) and bool(fmt.get("url"))


def _fmt_height(fmt: dict) -> int:
    return int(fmt.get("height") or 0)


def _fmt_ext(fmt: dict) -> str:
    return (fmt.get("ext") or "").lower()


def _fmt_vc(fmt: dict) -> str:
    return (fmt.get("vcodec") or "").lower()


def _fmt_ac(fmt: dict) -> str:
    return (fmt.get("acodec") or "").lower()


def _pick_by_itag_list(fmts: list, itags: list):
    by_id = {str(f.get("format_id")): f for f in fmts}
    for it in itags:
        f = by_id.get(str(it))
        if f and _is_direct(f):
            return f
    return None


def _pick_best_video_by_height(fmts: list, target_h: int):
    # фильтруем только видеопотоки (есть vcodec, нет аудио)
    candidates = [
        f for f in fmts
        if _is_direct(f) and _fmt_vc(f) != "none" and _fmt_ac(f) in ("", "none", None)
    ]
    if not candidates:
        return None

    # сортируем: приоритет тем, кто <= target_h; затем ближайший по высоте; авс1/авc1 предпочтительнее
    def key(f):
        h = _fmt_height(f)
        # близость к цели (меньше — лучше), но если выше цели — штраф
        over = 0 if h <= target_h else 1
        dist = abs(target_h - h)
        vc = _fmt_vc(f)
        # предпочтение avc1 (лучше для mp4 без перекодирования)
        pref = 0 if ("avc" in vc or "h264" in vc) else (1 if "vp9" in vc else 2)
        return (over, dist, pref, -int(f.get("tbr") or 0))

    candidates.sort(key=key)
    return candidates[0]


def _pick_best_audio(fmts: list):
    by_id = {str(f.get("format_id")): f for f in fmts}
    # 1) пробуем m4a/MP4 itag'и
    for it in AUDIO_ITAG_PREFERRED:
        f = by_id.get(it)
        if f and _is_direct(f) and _fmt_ac(f) != "none" and _fmt_vc(f) in ("", "none", None):
            return f

    # 2) иначе — любой direct-аудио с максимальным abr
    candidates = [
        f for f in fmts
        if _is_direct(f) and _fmt_ac(f) != "none" and _fmt_vc(f) in ("", "none", None)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda f: int(f.get("abr") or 0), reverse=True)
    return candidates[0]


# -------------------- Пул портов -------------------- #

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


# -------------------- Основной загрузчик -------------------- #

class YtDlpDownloader:
    _instance = None
    DOWNLOAD_DIR = '/downloads'

    MAX_RETRIES = 10

    # по умолчанию берём m4a для стабильного mux в MP4
    DEFAULT_AUDIO_ITAG = "140"

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
        self.port_pool = PortPool([9050 + i * 2 for i in range(20)])

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
        fut = loop.create_future()
        await self.queue.put((url, download_type, quality, fut, progress_msg))
        result = await fut
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
        """
        Теперь выбираем формат не по одному itag, а умно:
        - видео: список кандидатов для требуемой высоты, затем fallback по ближайшей высоте
        - аудио: m4a(140) в приоритете, затем лучший opus/m4a direct
        """
        start_proc = time.time()
        temp_paths = await self._prepare_temp_paths()
        result = None

        # высота как int
        try:
            target_h = int(quality)
        except Exception:
            target_h = 480

        try:
            info = await get_video_info_with_cache(url)
            fmts = _formats_from_info(info)

            if download_type == "audio":
                a_fmt = _pick_best_audio(fmts)
                if not a_fmt:
                    raise Exception("❌ Не найден подходящий аудиопоток (direct).")
                audio_url = a_fmt["url"]
                await self._download_with_tordl(audio_url, temp_paths['audio'], 'audio', progress_msg)
                result = temp_paths['audio']
            else:
                # VIDEO
                # 1) пробуем кандидатные itag для target_h
                cand_itags = VIDEO_ITAG_CANDIDATES.get(target_h) or []
                v_fmt = _pick_by_itag_list(fmts, cand_itags)
                if not v_fmt:
                    # 2) если не нашли — подбираем ближайший по высоте
                    v_fmt = _pick_best_video_by_height(fmts, target_h)
                if not v_fmt:
                    raise Exception(f"❌ Не найден подходящий видеопоток для {target_h}p (direct).")

                # AUDIO
                a_fmt = _pick_best_audio(fmts)
                if not a_fmt:
                    raise Exception("❌ Не найден подходящий аудиопоток (direct).")

                # Скачиваем параллельно
                video_url = v_fmt["url"]
                audio_url = a_fmt["url"]

                await asyncio.gather(
                    self._download_with_tordl(video_url, temp_paths['video'], 'video', progress_msg),
                    self._download_with_tordl(audio_url, temp_paths['audio'], 'audio', progress_msg),
                )
                # Склеиваем с учётом кодеков/контейнера
                result = await self._merge_files(temp_paths, v_fmt, a_fmt)

            return result
        finally:
            try:
                size = os.path.getsize(result) if result and os.path.exists(result) else 0
                duration = time.time() - start_proc
                avg = size / duration if duration > 0 else 0
                log_action(f"📈 Process: {download_type.upper()} {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
            except Exception:
                pass
            # чистим только времёнки, финальный output оставляем
            await self._cleanup_temp_files(temp_paths)

    async def _prepare_temp_paths(self):
        rnd = uuid.uuid4()
        base = {
            'video': os.path.join(self.DOWNLOAD_DIR, f"{rnd}_video"),
            'audio': os.path.join(self.DOWNLOAD_DIR, f"{rnd}_audio"),
            'output_base': os.path.join(self.DOWNLOAD_DIR, f"{rnd}"),
        }
        # расширения добавим позже на этапе merge
        return base

    async def _download_with_tordl(self, url, filename_noext, media_type, progress_msg):
        """
        Быстрая закачка через tor-dl с автоперезапуском (только прямые HTTP/HTTPS ссылки).
        """
        attempts = 0
        max_attempts = 3

        # временно сохраняем без расширения: tor-dl сам не требует правильного ext
        filename = filename_noext

        while attempts < max_attempts:
            attempts += 1
            port = await self.port_pool.get_next_port()
            log_action(f"🚀 {media_type.upper()} через порт {port} (попытка {attempts})")

            if platform.system() == 'Windows':
                executable = './tor-dl.exe'
            else:
                executable = './tor-dl'

            if not os.path.isfile(executable):
                raise FileNotFoundError(f"❌ Файл не найден: {executable}")

            if not os.access(executable, os.X_OK):
                os.chmod(executable, os.stat(executable).st_mode | stat.S_IEXEC)
                log_action(f"✅ Права на исполнение выданы: {executable}")

            cmd = [
                executable,
                '--tor-port', str(port),
                '--name', os.path.basename(os.path.abspath(filename)),
                '--destination', os.path.dirname(os.path.abspath(filename)),
                '--circuits', '50',
                '--min-lifetime', '10',
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

                if proc.returncode is None:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except:
                        pass

                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    if self._is_download_complete(filename, media_type):
                        size = os.path.getsize(filename)
                        duration = time.time() - start_time
                        speed = size / duration if duration > 0 else 0
                        log_action(f"✅ {media_type.upper()}: {size / 1024 / 1024:.1f}MB за {duration:.1f}s ({speed / 1024 / 1024:.1f} MB/s)")
                        return filename
                    else:
                        log_action(f"⚠️ {media_type.upper()}: неполная загрузка, перезапуск...")
                        continue

            except Exception as e:
                log_action(f"❌ Ошибка {media_type} попытка {attempts}: {e}")
                if proc.returncode is None:
                    proc.kill()

            await asyncio.sleep(1)

        raise Exception(f"Не удалось загрузить {media_type} за {max_attempts} попыток")

    def _is_download_complete(self, filename, media_type):
        try:
            size = os.path.getsize(filename)
            min_audio_size = 512 * 1024      # 0.5MB
            min_video_size = 5 * 1024 * 1024 # 5MB
            if media_type == 'audio':
                return size >= min_audio_size
            return size >= min_video_size
        except:
            return False

    async def _aggressive_monitor(self, proc, filename, start_time, media_type):
        last_size = 0
        last_change_time = start_time
        stall_threshold = 30
        check_interval = 3
        log_interval = 15
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
                        log_action(f"📊 {media_type}: {current_size / 1024 / 1024:.0f}MB | {speed / 1024 / 1024:.1f} MB/s")
                        last_log_time = current_time
                else:
                    if (current_time - last_change_time) > stall_threshold:
                        log_action(f"🔄 {media_type}: зависание {(current_time - last_change_time):.0f}с, перезапуск...")
                        return
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _merge_files(self, paths, v_fmt=None, a_fmt=None):
        """
        Устойчивый merge:
          1) пробуем MP4 + copy, если (video=h264/avc & audio=aac/m4a)
          2) пробуем MKV + copy (всегда совместим)
          3) fallback — перекодирование: либо audio→aac, либо полное (x264+aac)
        """
        vcodec = _fmt_vc(v_fmt or {})
        acodec = _fmt_ac(a_fmt or {})
        vext = _fmt_ext(v_fmt or {}) or "mp4"
        aext = _fmt_ext(a_fmt or {}) or "m4a"

        video_path = paths['video']
        audio_path = paths['audio']

        # добавим расширения для времёнок, чтобы ffmpeg не путался
        if not os.path.exists(video_path) and os.path.exists(video_path + f".{vext}"):
            video_path = video_path + f".{vext}"
        if not os.path.exists(audio_path) and os.path.exists(audio_path + f".{aext}"):
            audio_path = audio_path + f".{aext}"

        if os.path.exists(paths['video']) and not os.path.splitext(paths['video'])[1]:
            # если скачали без расширения — попробуем угадать
            new_v = paths['video'] + f".{vext}"
            try:
                os.rename(paths['video'], new_v)
                video_path = new_v
            except Exception:
                pass
        if os.path.exists(paths['audio']) and not os.path.splitext(paths['audio'])[1]:
            new_a = paths['audio'] + f".{aext}"
            try:
                os.rename(paths['audio'], new_a)
                audio_path = new_a
            except Exception:
                pass

        def run_ffmpeg(cmd):
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return proc.returncode, proc.stdout, proc.stderr

        # 1) MP4 + copy (если совместимо)
        output_mp4 = paths['output_base'] + ".mp4"
        if ("avc" in vcodec or "h264" in vcodec) and ("mp4a" in acodec or "aac" in acodec or aext == "m4a"):
            cmd1 = [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-i', video_path, '-i', audio_path,
                '-c:v', 'copy', '-c:a', 'copy',
                '-map', '0:v:0', '-map', '1:a:0',
                '-y', output_mp4
            ]
            rc, _, err = run_ffmpeg(cmd1)
            if rc == 0 and os.path.exists(output_mp4) and os.path.getsize(output_mp4) > 0:
                log_action(f"✅ Output: {output_mp4}")
                return output_mp4
            else:
                log_action(f"⚠️ MP4 copy не удалось, пробуем MKV. FFmpeg: {err.decode(errors='ignore')[:300]}")

        # 2) MKV + copy (универсально)
        output_mkv = paths['output_base'] + ".mkv"
        cmd2 = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-i', video_path, '-i', audio_path,
            '-c:v', 'copy', '-c:a', 'copy',
            '-map', '0:v:0', '-map', '1:a:0',
            '-y', output_mkv
        ]
        rc, _, err = run_ffmpeg(cmd2)
        if rc == 0 and os.path.exists(output_mkv) and os.path.getsize(output_mkv) > 0:
            log_action(f"✅ Output: {output_mkv}")
            return output_mkv
        else:
            log_action(f"⚠️ MKV copy не удалось, пробуем перекодирование аудио. FFmpeg: {err.decode(errors='ignore')[:300]}")

        # 3) перекодирование аудио → aac (сохраним h264, если он уже h264; иначе полный транскод)
        if "avc" in vcodec or "h264" in vcodec:
            output_mp4_aac = paths['output_base'] + ".mp4"
            cmd3 = [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-i', video_path, '-i', audio_path,
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                '-map', '0:v:0', '-map', '1:a:0',
                '-y', output_mp4_aac
            ]
            rc, _, err = run_ffmpeg(cmd3)
            if rc == 0 and os.path.exists(output_mp4_aac) and os.path.getsize(output_mp4_aac) > 0:
                log_action(f"✅ Output: {output_mp4_aac}")
                return output_mp4_aac
            else:
                log_action(f"⚠️ copy+AAC не удалось, пробуем полный транскод. FFmpeg: {err.decode(errors='ignore')[:300]}")

        # Полный транскод (на крайний случай)
        output_mp4_full = paths['output_base'] + ".mp4"
        cmd4 = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-i', video_path, '-i', audio_path,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
            '-c:a', 'aac', '-b:a', '192k',
            '-map', '0:v:0', '-map', '1:a:0',
            '-y', output_mp4_full
        ]
        rc, _, err = run_ffmpeg(cmd4)
        if rc != 0:
            log_action(f"❌ FFmpeg error {rc}: {err.decode(errors='ignore')[:500]}")
            raise subprocess.CalledProcessError(rc, cmd4, _, err)
        log_action(f"✅ Output: {output_mp4_full}")
        return output_mp4_full

    async def _cleanup_temp_files(self, paths):
        for k in ['video', 'audio']:
            fp = paths.get(k)
            if not fp:
                continue
            # удаляем обе версии (с расширением/без)
            for cand in (fp, fp + ".mp4", fp + ".webm", fp + ".m4a", fp + ".mkv"):
                try:
                    if cand and os.path.exists(cand):
                        os.remove(cand)
                        log_action(f"🧹 Удален временный файл: {cand}")
                except Exception:
                    pass

    async def stop(self):
        if self.is_running:
            self.is_running = False
            for task in self.active_tasks:
                task.cancel()
            await asyncio.gather(*self.active_tasks, return_exceptions=True)
            log_action("🛑 Все воркеры остановлены")
