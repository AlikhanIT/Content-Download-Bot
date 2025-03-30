import shutil
import ffmpeg
import yt_dlp
import requests
from PIL import Image
import io
from urllib.parse import urlparse, parse_qs
from aiogram.client.session import aiohttp

# 📹 Получаем доступные разрешения и размеры видео
async def get_video_resolutions_and_sizes(url):
    try:
        proxy = await get_proxy()

        ydl_opts = {
            'proxy': proxy['url'] if proxy else None,
            'skip_download': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            formats = info_dict.get("formats", [])
            resolution_sizes = {}
            for fmt in formats:
                width = fmt.get("width")
                height = fmt.get("height")
                filesize = fmt.get("filesize")
                if width and height and filesize:
                    resolution = f"{width}x{height}"
                    filesize_mb = float(filesize) / (1024 * 1024)
                    resolution_sizes[resolution] = max(resolution_sizes.get(resolution, 0), filesize_mb)
            return resolution_sizes
    except Exception as e:
        log_action(f"❌ Ошибка получения разрешений видео: {e}")
        return {}

# 🖼️ Загрузка и оптимизация превью
async def get_thumbnail_bytes(url):
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            img = Image.open(io.BytesIO(response.content))
            img = img.convert("RGB")
            img.thumbnail((320, 320))
            byte_io = io.BytesIO()
            img.save(byte_io, format="JPEG", optimize=True, quality=85)
            byte_io.seek(0)
            return byte_io
        return None
    except Exception as e:
        log_action(f"❌ Ошибка загрузки превью: {e}")
        return None

# 📄 Получаем информацию о видео (ID, название, превью)
def extract_video_id(url):
    parsed = urlparse(url)
    if 'youtube.com' in parsed.netloc:
        if parsed.path.startswith('/shorts/'):
            return parsed.path.split('/shorts/')[1].split('/')[0]
        query = parse_qs(parsed.query)
        return query.get('v', [None])[0]
    elif 'youtu.be' in parsed.netloc:
        return parsed.path.strip('/')
    return None

async def get_video_info(url):
    log_action('⚡ Быстрое получение информации о видео через oEmbed')

    try:
        video_id = extract_video_id(url)
        if not video_id:
            log_action('❌ Не удалось извлечь ID видео')
            return None, None, None

        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"

        async with aiohttp.ClientSession() as session:
            async with session.get(oembed_url) as resp:
                if resp.status != 200:
                    raise Exception(f"Ошибка oEmbed: {resp.status}")
                data = await resp.json()

        title = data.get("title", "Видео")
        thumbnail_url = data.get("thumbnail_url")

        return video_id, title, thumbnail_url

    except Exception as e:
        log_action(f"❌ Ошибка получения информации о видео: {e}")
        return None, None, None

def check_ffmpeg_installed():
    if not shutil.which("ffmpeg"):
        raise EnvironmentError("FFmpeg не установлен. Установите его и добавьте в PATH.")


async def get_video_resolution(video_path):
    try:
        # Используем ffmpeg.probe для получения метаданных видео
        probe = ffmpeg.probe(video_path)
        video_stream = next((stream for stream in probe["streams"] if stream["codec_type"] == "video"), None)
        if video_stream:
            width = int(video_stream["width"])
            height = int(video_stream["height"])
            return width, height
        return None, None  # Если видеострим не найден
    except Exception as e:
        print(f"Ошибка при получении разрешения видео: {e} {video_path}")
        return None, None  # В случае ошибки

async def get_proxy():
    proxy = {'ip': '127.0.0.1', 'port': '9050', 'user': '', 'password': ''}  # Tor по умолчанию
    proxy_url = f"socks5://{proxy['ip']}:{proxy['port']}"
    log_action(f"🛡 Используется прокси: {proxy_url}")
    return {
        'url': proxy_url,
        'key': f"{proxy['ip']}:{proxy['port']}"
    }
import asyncio
import time
import json
from asyncio import Lock
from bot.utils.log import log_action

# Cache for full yt-dlp dump-json result
_video_info_cache = {}
_cache_lock = Lock()
_cache_events = {}
_CACHE_TTL_SECONDS = 2 * 60 * 60  # 2 hours


async def get_video_info_with_cache(video_url, max_retries=10, delay=5):
    key = (video_url,)
    log_action(f"📦 Проверка кэша: {video_url}")

    wait_for_other = False

    async with _cache_lock:
        entry = _video_info_cache.get(key)
        if entry:
            info, expire_time = entry
            if time.time() < expire_time:
                log_action(f"📦 Взято из кэша yt-dlp JSON: {video_url}")
                return info
            else:
                _video_info_cache.pop(key, None)

        if key in _cache_events:
            event = _cache_events[key]
            wait_for_other = True
        else:
            event = asyncio.Event()
            _cache_events[key] = event

    if wait_for_other:
        log_action(f"⏳ Ожидание кэша от другого потока: {video_url}")
        await event.wait()
        async with _cache_lock:
            entry = _video_info_cache.get(key)
            if entry:
                return entry[0]
            else:
                raise Exception("❌ Не удалось получить данные из кэша после ожидания")

    try:
        for attempt in range(1, max_retries + 1):
            try:
                from bot.utils.downloader import YtDlpDownloader
                proxy = await YtDlpDownloader()._get_proxy()
                user_agent = YtDlpDownloader().user_agent.random

                cmd = [
                    "yt-dlp",
                    "--skip-download",
                    "--no-playlist",
                    "--no-warnings",
                    f"--proxy={proxy['url']}",
                    f"--user-agent={user_agent}",
                    "--dump-json",
                    video_url
                ]

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    raise Exception(f"❌ yt-dlp error:\n{stderr.decode()}")

                raw_output = stdout.decode().strip()
                if not raw_output:
                    raise Exception("⚠️ yt-dlp не вернул данных.")

                info = json.loads(raw_output)

                async with _cache_lock:
                    _video_info_cache[key] = (info, time.time() + _CACHE_TTL_SECONDS)
                    log_action(f"💾 Сохранено yt-dlp JSON: {video_url}")
                return info

            except Exception as e:
                err_msg = str(e)
                retriable = (
                    "403" in err_msg or
                    "429" in err_msg or
                    "not a bot" in err_msg.lower()
                )
                if retriable:
                    log_action(f"⚠️ Попытка {attempt}/{max_retries} — ошибка: {err_msg.splitlines()[0]}")
                    if attempt < max_retries:
                        await asyncio.sleep(delay)
                        continue
                raise e
    finally:
        async with _cache_lock:
            if key in _cache_events:
                _cache_events[key].set()
                _cache_events.pop(key, None)


async def extract_url_from_info(info, itags, fallback_itags=None):
    fallback_itags = fallback_itags or []
    formats = info.get("formats", [])
    format_map = {f["format_id"]: f["url"] for f in formats if "url" in f}

    for tag in itags:
        if str(tag) in format_map:
            log_action(f"🔗 Найдена ссылка по itag={tag}")
            return format_map[str(tag)]

    for fallback in fallback_itags:
        if str(fallback) in format_map:
            log_action(f"🔁 Использован fallback itag={fallback}")
            return format_map[str(fallback)]

    raise Exception(f"❌ Не найдены подходящие itag: {itags} (fallback: {fallback_itags})")
