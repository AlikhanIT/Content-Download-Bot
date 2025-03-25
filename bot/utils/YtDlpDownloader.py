import yt_dlp
import asyncio
import os
import uuid
import subprocess
from functools import cached_property
from fake_useragent import UserAgent
from bot.proxy.proxy_manager import get_available_proxy, ban_proxy
from bot.utils.log import log_action

class YtDlpDownloader:
    _instance = None
    DOWNLOAD_DIR = '/downloads'
    QUALITY_ITAG_MAP = {
        "144": "160", "240": "133", "360": "134", "480": "135",
        "720": "136", "1080": "137", "1440": "264", "2160": "266"
    }
    DEFAULT_VIDEO_ITAG = "243"
    DEFAULT_AUDIO_ITAG = "249"
    MAX_RETRIES = 5  # Количество попыток скачивания

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
        if not os.path.exists(self.DOWNLOAD_DIR):
            os.makedirs(self.DOWNLOAD_DIR)
            log_action(f"📂 Создана папка для загрузки: {self.DOWNLOAD_DIR}")

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
            try:
                url, download_type, quality, future = await self.queue.get()
                result = await self._process_download(url, download_type, quality)
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
            finally:
                self.queue.task_done()

    def _run_ydl_download(self, url, opts, media_type):
        log_action(f"🎯 Скачивание {media_type} (itag: {opts['format']})")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    async def _process_download(self, url, download_type, quality   ):
        file_paths = None  # Инициализация переменной
        try:
            file_paths = await self._prepare_file_paths(download_type)
            proxy = await self._get_proxy()

            if download_type == "audio":
                output = await self._download_only_audio(url, file_paths['audio'], proxy)
            else:
                await self._download_video(url, file_paths['video'], quality)
                await self._download_audio(url, file_paths['audio'])
                output = await self._merge_files(file_paths)

            return output
        except Exception as e:
            raise e
        finally:
            if file_paths and download_type != 'audio':  # Проверка на существование
                await self._cleanup_temp_files(file_paths, download_type)






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

        # Проверка на существование файлов
        if not os.path.exists(file_paths['video']):
            log_action(f"❌ Видео файл отсутствует: {file_paths['video']}")
            raise FileNotFoundError(f"Видео файл не найден: {file_paths['video']}")

        if not os.path.exists(file_paths['audio']):
            log_action(f"❌ Аудио файл отсутствует: {file_paths['audio']}")
            raise FileNotFoundError(f"Аудио файл не найден: {file_paths['audio']}")

        try:
            command = [
                'ffmpeg', '-y',
                '-i', file_paths['video'],
                '-i', file_paths['audio'],
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-strict', 'experimental',
                file_paths['output']
            ]
            subprocess.run(command, check=True)
            log_action(f"✅ Готовый файл: {file_paths['output']}")
            return file_paths['output']
        except subprocess.CalledProcessError as e:
            log_action(f"❌ Ошибка объединения файлов: {e}")
            raise

    def _handle_progress(self, d):
        status = d['status']
        log_action(f"📊 Статус: {status.upper()}")

        if status == 'downloading':
            speed = d.get('speed', 0) or 0
            eta = d.get('eta', 0) or 0
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded_bytes = d.get('downloaded_bytes', 0) or 0
            percent = (downloaded_bytes / total_bytes * 100) if total_bytes else 0

            log_action(
                f"⬇️ Скачивание: {percent:.2f}% | "
                f"Размер: {total_bytes / (1024 * 1024):.2f} MB | "
                f"Загружено: {downloaded_bytes / (1024 * 1024):.2f} MB | "
                f"Скорость: {speed / (1024 * 1024):.2f} MB/s | "
                f"Осталось: {eta}s"
            )

        elif status == 'finished':
            log_action(f"✅ Скачивание завершено: {d.get('filename', 'Файл не указан')}")
        elif status == 'error':
            log_action(f"❌ Ошибка загрузки: {d.get('error', 'Неизвестная ошибка')}")
        else:
            log_action(f"ℹ️ Дополнительный статус: {d}")

    def _log_download_progress(self, d):
        speed = d.get('speed', 0) or 0
        eta = d.get('eta', 0) or 0
        total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        downloaded_bytes = d.get('downloaded_bytes', 0) or 0
        percent = (downloaded_bytes / total_bytes * 100) if total_bytes else 0

        log_action(
            f"⬇️ Скачивание: {percent:.2f}% | "
            f"Размер: {total_bytes / (1024 ** 2):.2f} MB | "
            f"Загружено: {downloaded_bytes / (1024 ** 2):.2f} MB | "
            f"Скорость: {speed / (1024 ** 2):.2f} MB/s | "
            f"Осталось: {eta}s"
        )

    async def _cleanup_temp_files(self, file_paths, download_type):
        try:
            if download_type != "audio":
                for key in ['video', 'audio']:
                    if os.path.exists(file_paths[key]):
                        os.remove(file_paths[key])
                        log_action(f"🧹 Удален временный файл: {file_paths[key]}")
        except Exception as e:
            log_action(f"⚠️ Ошибка при очистке файлов: {e}")

    async def _download_only_audio(self, url, output_path, proxy):
        log_action("🎧 Скачивание только аудио")
        opts = self._get_audio_only_opts(output_path, proxy)

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        return output_path

    async def _download_video(self, url, output_path, quality):
        itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
        return await self._download_with_retries(url, output_path, "video", itag)

    async def _download_audio(self, url, output_path):
        return await self._download_with_retries(url, output_path, "audio", self.DEFAULT_AUDIO_ITAG)

    async def _download_with_retries(self, url, output_path, download_type, itag=None):
        for attempt in range(self.MAX_RETRIES):
            proxy = await self._get_proxy()
            try:
                opts = self._get_ydl_opts(output_path, itag, proxy)
                await asyncio.to_thread(self._run_ydl_download, url, opts,
                                        download_type)  # Запускаем в отдельном потоке
                return output_path
            except Exception as e:
                log_action(f"❌ Попытка {attempt + 1} не удалась: {e}")
                if proxy:
                    ban_proxy(proxy['url'])
                await asyncio.sleep(2)  # Ожидание перед новой попыткой
        raise Exception("⚠️ Все попытки скачивания исчерпаны")

    async def _get_proxy(self):
        proxy = {'ip': '127.0.0.1', 'port': '9050', 'user': '', 'password': ''}  # Tor по умолчанию
        proxy_url = f"socks5://{proxy['ip']}:{proxy['port']}"
        log_action(f"🛡 Используется прокси: {proxy_url}")
        return {
            'url': proxy_url,
            'key': f"{proxy['ip']}:{proxy['port']}"
        }

    def _get_ydl_opts(self, output_path, itag, proxy):
        return {
            'format': itag,
            'outtmpl': output_path,
            'merge_output_format': 'mp4',
            'progress_hooks': [self._handle_progress],
            'noprogress': False,
            'retries': 30,
            'socket_timeout': 600,
            'continuedl': True,
            'fragment_retries': 30,
            'verbose': True,
            'print': log_action,
            'forceipv4': True,
            'nocheckcertificate': True,
            'User-Agent': self.user_agent.random,
            'proxy': proxy['url'] if proxy else None,
            'force_ipv6': False,
            'cmdline_args': ['-4'],
        }

    def _get_audio_only_opts(self, output_path, proxy):
        return {
            'format': 'bestaudio/best',
            'outtmpl': output_path,
            'progress_hooks': [self._handle_progress],
            'retries': 30,
            'fragment_retries': 30,
            'print': log_action,
            'forceipv4': True,
            'socket_timeout': 600,
            'nocheckcertificate': True,
            'noprogress': False,
            'continuedl': True,
            'User-Agent': self.user_agent.random,
            'proxy': proxy['url'] if proxy else None,
            'quiet': False,
            'verbose': True
        }

    def _run_ydl_download(self, url, opts, media_type):
        log_action(f"🎯 Скачивание {media_type} (itag: {opts['format']})")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])