#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import platform
import stat
import uuid
import subprocess
import asyncio
import time
import json
from functools import cached_property

import aiofiles
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
    # Multiple audio format fallbacks
    AUDIO_ITAGS = ["249", "250", "251", "140", "139"]  # WebM Opus, AAC formats
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
        self._loop = None
        # Простой round-robin пул портов
        self.port_pool = PortPool([9050 + i * 2 for i in range(20)])

    def _ensure_download_dir(self):
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        log_action(f"📂 Папка для загрузки: {self.DOWNLOAD_DIR}")

    @cached_property
    def user_agent(self):
        return UserAgent()

    async def start_workers(self):
        if not self.is_running:
            self._loop = asyncio.get_running_loop()
            self.is_running = True
            for _ in range(self.max_threads):
                task = asyncio.create_task(self._worker())
                self.active_tasks.add(task)
                task.add_done_callback(self.active_tasks.discard)
            log_action(f"🚀 Запущено {self.max_threads} воркеров")

    async def download(self, url, download_type="video", quality="480", progress_msg=None):
        log_action(f"🎯 Начинаем загрузку: {url} [{download_type}/{quality}]")
        start_time = time.time()
        await self.start_workers()

        # Ensure we're using the same event loop
        current_loop = asyncio.get_running_loop()
        if self._loop and self._loop != current_loop:
            log_action("⚠️ Event loop mismatch detected, reinitializing...")
            await self.stop()
            await self.start_workers()

        loop = asyncio.get_event_loop()
        future = loop.create_future()

        log_action(f"📋 Добавляем задачу в очередь (размер: {self.queue.qsize()})")
        await self.queue.put((url, download_type, quality, future, progress_msg))

        log_action("⏳ Ожидаем выполнения задачи...")
        try:
            result = await asyncio.wait_for(future, timeout=300)  # 5 минут таймаут
            log_action(f"✅ Задача завершена: {result}")
        except asyncio.TimeoutError:
            log_action("❌ Таймаут загрузки (5 минут)")
            raise Exception("Download timeout after 5 minutes")

        try:
            size = os.path.getsize(result)
            duration = time.time() - start_time
            avg = size / duration if duration > 0 else 0
            log_action(f"📊 Finished: {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
        except Exception:
            pass
        return result

    async def _worker(self):
        worker_id = id(asyncio.current_task())
        log_action(f"👷 Воркер {worker_id} запущен")

        while True:
            try:
                log_action(f"👷 Воркер {worker_id} ожидает задачу...")
                url, download_type, quality, future, progress_msg = await self.queue.get()
                log_action(f"👷 Воркер {worker_id} получил задачу: {url}")

                try:
                    res = await self._process_download(url, download_type, quality, progress_msg)
                    if not future.done():
                        future.set_result(res)
                        log_action(f"👷 Воркер {worker_id} завершил задачу успешно")
                except Exception as e:
                    log_action(f"👷 Воркер {worker_id} получил ошибку: {e}")
                    if not future.done():
                        future.set_exception(e)
                finally:
                    self.queue.task_done()

            except asyncio.CancelledError:
                log_action(f"👷 Воркер {worker_id} отменен")
                break
            except Exception as e:
                log_action(f"❌ Worker {worker_id} critical error: {e}")

    async def _process_download(self, url, download_type, quality, progress_msg):
        log_action(f"🔄 Начинаем обработку: {download_type} {quality}")
        start_proc = time.time()
        file_paths = await self._prepare_file_paths(download_type)
        result = None

        try:
            log_action("📺 Получаем информацию о видео...")
            info = await get_video_info_with_cache(url)
            log_action("✅ Информация о видео получена")

            if download_type == "audio":
                log_action("🎵 Ищем URL аудио...")
                audio_url = await self._extract_audio_url_with_fallback(info)
                log_action(f"🎵 URL аудио найден: {audio_url[:100]}...")
                result = await self._download_with_tordl(audio_url, file_paths['audio'], 'audio', progress_msg)
            else:
                log_action(f"🎬 Ищем URL видео (качество: {quality})...")
                itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
                log_action(f"🎬 Используем itag: {itag}")

                video_url = await extract_url_from_info(info, [itag])
                log_action(f"🎬 URL видео найден: {video_url[:100]}...")

                audio_url = await self._extract_audio_url_with_fallback(info)
                log_action(f"🎵 URL аудио найден: {audio_url[:100]}...")

                log_action("⬇️ Начинаем параллельную загрузку видео и аудио...")
                await asyncio.gather(
                    self._download_with_tordl(video_url, file_paths['video'], 'video', progress_msg),
                    self._download_with_tordl(audio_url, file_paths['audio'], 'audio', progress_msg)
                )
                log_action("✅ Загрузка завершена, начинаем объединение...")
                result = await self._merge_files(file_paths)

            return result
        except Exception as e:
            log_action(f"❌ Ошибка в _process_download: {e}")
            raise
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

    async def _extract_audio_url_with_fallback(self, info):
        """Try multiple audio formats with fallback"""
        log_action(f"🔍 Пробуем аудио форматы: {self.AUDIO_ITAGS}")
        for itag in self.AUDIO_ITAGS:
            try:
                log_action(f"🔍 Пробуем итаг {itag}...")
                audio_url = await extract_url_from_info(info, [itag])
                log_action(f"✅ Found audio format: {itag}")
                return audio_url
            except Exception as e:
                log_action(f"⚠️ Audio format {itag} not available: {e}")
                continue

        # If no audio formats work, raise an exception
        raise Exception(f"❌ No suitable audio formats found. Tried: {self.AUDIO_ITAGS}")

    async def _prepare_file_paths(self, download_type):
        rnd = uuid.uuid4()
        base = {'output': os.path.join(self.DOWNLOAD_DIR, f"{rnd}.mp4")}
        if download_type == 'audio':
            base['audio'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}.m4a")
        else:
            base['video'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_video.mp4")
            base['audio'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_audio.m4a")

        log_action(f"📁 Подготовлены пути файлов: {base}")
        return base

    async def _download_with_tordl(self, url, filename, media_type, progress_msg):
        """Максимально быстрая загрузка через tor-dl с автоперезапуском"""
        log_action(f"🚀 Начинаем загрузку {media_type}: {filename}")
        attempts = 0
        max_attempts = 3

        while attempts < max_attempts:
            attempts += 1
            port = await self.port_pool.get_next_port()

            log_action(f"🚀 {media_type.upper()} через порт {port} (попытка {attempts})")

            # Проверяем tor-dl
            if platform.system() == 'Windows':
                executable = './tor-dl.exe'
            else:
                executable = './tor-dl'

            log_action(f"🔍 Проверяем исполняемый файл: {executable}")

            if not os.path.isfile(executable):
                raise FileNotFoundError(f"❌ Файл не найден: {executable}")

            if not os.access(executable, os.X_OK):
                log_action(f"🔧 Выдаем права на исполнение...")
                os.chmod(executable, os.stat(executable).st_mode | stat.S_IEXEC)
                log_action(f"✅ Права на исполнение выданы: {executable}")

            if not os.access(executable, os.X_OK):
                raise PermissionError(f"❌ Нет прав на исполнение: {executable}")

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

            log_action(f"🔧 Команда: {' '.join(cmd)}")
            start_time = time.time()

            # Проверяем, что директория существует
            os.makedirs(os.path.dirname(filename), exist_ok=True)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                log_action(f"🚀 Процесс tor-dl запущен (PID: {proc.pid})")

                # Мониторинг с логированием
                monitor_task = asyncio.create_task(
                    self._aggressive_monitor(proc, filename, start_time, media_type)
                )

                # Ждем завершения процесса или мониторинга
                done, pending = await asyncio.wait(
                    [asyncio.create_task(proc.wait()), monitor_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Отменяем незавершенные задачи
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Получаем stdout/stderr
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
                    if stdout:
                        log_action(f"📝 tor-dl stdout: {stdout.decode()}")
                    if stderr:
                        log_action(f"📝 tor-dl stderr: {stderr.decode()}")
                except:
                    pass

                # Убиваем процесс если он еще работает
                if proc.returncode is None:
                    log_action(f"🔪 Убиваем процесс {proc.pid}")
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except:
                        pass

                log_action(f"🏁 Процесс завершен с кодом: {proc.returncode}")

                # Проверяем результат
                if os.path.exists(filename):
                    size = os.path.getsize(filename)
                    log_action(f"📊 Файл существует, размер: {size} байт")

                    if size > 0:
                        duration = time.time() - start_time
                        speed = size / duration if duration > 0 else 0

                        # Проверяем что файл действительно полностью скачался
                        if self._is_download_complete(filename, media_type):
                            log_action(
                                f"✅ {media_type.upper()}: {size / 1024 / 1024:.1f}MB за {duration:.1f}s ({speed / 1024 / 1024:.1f} MB/s)")
                            return filename
                        else:
                            log_action(f"⚠️ {media_type.upper()}: неполная загрузка, перезапуск...")
                            continue
                    else:
                        log_action(f"⚠️ Файл пустой, размер: 0 байт")
                else:
                    log_action(f"❌ Файл не создан: {filename}")

            except Exception as e:
                log_action(f"❌ Ошибка {media_type} попытка {attempts}: {e}")
                if 'proc' in locals() and proc.returncode is None:
                    proc.kill()

            # Короткая пауза перед следующей попыткой
            log_action(f"⏳ Пауза перед попыткой {attempts + 1}...")
            await asyncio.sleep(2)

        raise Exception(f"Не удалось загрузить {media_type} за {max_attempts} попыток")

    def _is_download_complete(self, filename, media_type):
        """Проверка что файл полностью скачался"""
        try:
            size = os.path.getsize(filename)
            # Минимальные размеры для проверки
            min_audio_size = 100 * 1024  # 100KB для аудио (уменьшено для тестов)
            min_video_size = 1 * 1024 * 1024  # 1MB для видео (уменьшено для тестов)

            if media_type == 'audio':
                result = size >= min_audio_size
            else:
                result = size >= min_video_size

            log_action(
                f"🔍 Проверка завершенности {media_type}: {size} байт, минимум: {min_audio_size if media_type == 'audio' else min_video_size}, результат: {result}")
            return result
        except Exception as e:
            log_action(f"❌ Ошибка проверки файла: {e}")
            return False

    async def _aggressive_monitor(self, proc, filename, start_time, media_type):
        """Агрессивный мониторинг с быстрым обнаружением зависания"""
        log_action(f"📊 Запускаем мониторинг для {media_type}")
        last_size = 0
        last_change_time = start_time
        stall_threshold = 45  # 45 секунд без изменений = зависание
        check_interval = 5  # Проверяем каждые 5 секунд
        log_interval = 15  # Логируем каждые 15 секунд
        last_log_time = start_time

        while proc.returncode is None:
            try:
                await asyncio.sleep(check_interval)
                current_time = time.time()

                if not os.path.exists(filename):
                    elapsed = current_time - start_time
                    if elapsed > 30:  # Если файл не появился за 30 секунд
                        log_action(f"⚠️ {media_type}: файл не создан за {elapsed:.0f}с, возможно зависание...")
                    continue

                current_size = os.path.getsize(filename)

                # Проверяем прогресс
                if current_size > last_size:
                    last_size = current_size
                    last_change_time = current_time

                    # Логируем прогресс
                    if current_time - last_log_time >= log_interval:
                        elapsed = current_time - start_time
                        speed = current_size / elapsed if elapsed > 0 else 0
                        log_action(
                            f"📊 {media_type}: {current_size / 1024 / 1024:.0f}MB | {speed / 1024 / 1024:.1f} MB/s")
                        last_log_time = current_time
                else:
                    # Нет прогресса - проверяем зависание
                    stall_time = current_time - last_change_time
                    if stall_time > stall_threshold:
                        log_action(f"🔄 {media_type}: зависание {stall_time:.0f}с, перезапуск...")
                        return  # Выходим из мониторинга для перезапуска

            except asyncio.CancelledError:
                log_action(f"📊 Мониторинг {media_type} отменен")
                break
            except Exception as e:
                log_action(f"❌ Ошибка мониторинга {media_type}: {e}")

    async def _merge_files(self, file_paths):
        log_action("🔄 Объединение видео и аудио...")

        # Проверяем что файлы существуют
        for key in ['video', 'audio']:
            if not os.path.exists(file_paths[key]):
                raise Exception(f"❌ Файл {key} не найден: {file_paths[key]}")
            size = os.path.getsize(file_paths[key])
            log_action(f"📊 {key} файл: {size / 1024 / 1024:.2f} MB")

        cmd = [
            'ffmpeg', '-i', file_paths['video'], '-i', file_paths['audio'],
            '-c:v', 'copy', '-c:a', 'copy', '-map', '0:v:0', '-map', '1:a:0',
            '-y', file_paths['output']
        ]

        log_action(f"🔧 FFmpeg команда: {' '.join(cmd)}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()

        if proc.returncode != 0:
            log_action(f"❌ FFmpeg error {proc.returncode}")
            log_action(f"❌ FFmpeg stdout: {out.decode()}")
            log_action(f"❌ FFmpeg stderr: {err.decode()}")
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)

        # Проверяем результат
        if os.path.exists(file_paths['output']):
            size = os.path.getsize(file_paths['output'])
            log_action(f"✅ Output: {file_paths['output']} ({size / 1024 / 1024:.2f} MB)")
        else:
            raise Exception("❌ Выходной файл не создан")

        return file_paths['output']

    async def _cleanup_temp_files(self, file_paths):
        for key in ['video', 'audio']:
            fp = file_paths.get(key)
            if fp and os.path.exists(fp):
                os.remove(fp)
                log_action(f"🧹 Удален временный файл: {fp}")

    async def stop(self):
        """Остановка всех воркеров"""
        if self.is_running:
            log_action("🛑 Останавливаем воркеры...")
            self.is_running = False
            for task in self.active_tasks:
                task.cancel()
            await asyncio.gather(*self.active_tasks, return_exceptions=True)
            self.active_tasks.clear()
            log_action("🛑 Все воркеры остановлены")


def safe_log(msg):
    tqdm.write(msg)
    log_action(msg)