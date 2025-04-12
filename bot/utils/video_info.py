import shutil
import ffmpeg
import yt_dlp
import requests
from PIL import Image
import io
from urllib.parse import urlparse, parse_qs
from aiogram.client.session import aiohttp
import asyncio
import time
import json
from asyncio import Lock
from bot.utils.log import log_action

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

# 🖼️ Загрузка превью без изменений
async def get_thumbnail_bytes(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log_action(f"❌ Не удалось получить превью, статус: {resp.status}")
                    return None
                content = await resp.read()

        # Просто открываем и сохраняем без изменений
        img = Image.open(io.BytesIO(content)).convert("RGB")
        byte_io = io.BytesIO()
        img.save(byte_io, format="JPEG", optimize=True)
        byte_io.seek(0)
        return byte_io

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



# Cache for full yt-dlp dump-json result
_video_info_cache = {}
_cache_lock = Lock()
_cache_events = {}
_CACHE_TTL_SECONDS = 2 * 60 * 60  # 2 hours

async def get_video_info_with_cache(video_url, delay=2):
    from bot.utils.downloader import YtDlpDownloader
    from bot.utils.log import log_action
    import random

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
        banned_ports = {}
        TOR_INSTANCES = 50
        ports = [9050 + i * 2 for i in range(TOR_INSTANCES)]

        attempt = 0
        use_proxy = False  # <-- сначала без прокси

        while True:
            attempt += 1
            user_agent = YtDlpDownloader().user_agent.random

            if use_proxy:
                available_ports = [p for p in ports if banned_ports.get(p, 0) < time.time()]
                if not available_ports:
                    raise Exception("❌ Все Tor-порты временно забанены")

                port = random.choice(available_ports)
                proxy_url = f"socks5://127.0.0.1:{port}"
                log_action(f"🚀 Попытка {attempt} через порт {port}")
            else:
                proxy_url = None
                log_action(f"🚀 Попытка {attempt} без прокси")

            cmd = [
                "yt-dlp",
                "--skip-download",
                "--no-playlist",
                "--no-warnings",
                "--user-agent", user_agent,
                "--dump-json",
                video_url
            ]
            if proxy_url:
                cmd.append(f"--proxy={proxy_url}")

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
                except asyncio.TimeoutError:
                    log_action(f"⏱️ Таймаут при попытке через порт {port if use_proxy else 'без прокси'} — бан на 5 мин")
                    if use_proxy:
                        banned_ports[port] = time.time() + 300
                    use_proxy = True  # после первой неудачи всегда с прокси
                    continue

                if proc.returncode != 0:
                    err = stderr.decode().strip()
                    if any(code in err for code in ["403", "429"]):
                        if use_proxy:
                            banned_ports[port] = time.time() + 600
                            log_action(f"🚫 Порт {port} забанен на 10 минут из-за ошибки {err[:80]}")
                        use_proxy = True
                        continue
                    log_action(f"❌ yt-dlp error: {err.splitlines()[0] if err else 'unknown error'}")
                    raise Exception(f"❌ yt-dlp error:\n{err}")

                raw_output = stdout.decode().strip()
                if not raw_output:
                    raise Exception("⚠️ yt-dlp не вернул данных.")

                info = json.loads(raw_output)
                async with _cache_lock:
                    _video_info_cache[key] = (info, time.time() + _CACHE_TTL_SECONDS)
                    log_action(f"📂 Сохранено yt-dlp JSON: {video_url}")
                return info

            except Exception as e:
                err_str = str(e).lower()
                retriable = any(code in err_str for code in ["403", "429", "not a bot", "player response"])
                log_action(f"⚠️ Попытка {attempt} — ошибка: {str(e).splitlines()[0]}")
                use_proxy = True
                if retriable:
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

import aiohttp
import asyncio
from bot.utils.log import log_action  # Убедись, что путь правильный

async def resolve_final_url(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://www.youtube.com/",
        "Range": "bytes=0-1023"
    }

    visited = set()
    step = 0

    while True:
        if url in visited:
            log_action(f"🔁 Циклический редирект — повтор: {url}")
            return None
        visited.add(url)
        step += 1

        log_action(f"\n🔎 Шаг {step}")
        log_action(f"🌍 Запрос: {url}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, allow_redirects=False) as response:
                    log_action(f"📦 Статус: {response.status}")
                    log_action("🔐 Заголовки:")
                    for k, v in response.headers.items():
                        log_action(f"  {k}: {v}")

                    # Проверка Location
                    location = response.headers.get("Location")
                    if location:
                        log_action(f"➡️ Переход по Location: {location}")
                        url = location
                        continue

                    # Чтение тела
                    content = await response.content.read(1024)
                    content_preview = content.decode(errors="replace")
                    log_action(f"📄 Первые 1024 байта тела:\n{content_preview}")

                    log_action(f"✅ Финальный URL: {url}")
                    return url

        except aiohttp.ClientResponseError as e:
            log_action(f"❌ HTTP ошибка: {e.status} — {e.message}")
            return None
        except aiohttp.ClientConnectionError as e:
            log_action(f"❌ Ошибка соединения: {e}")
            return None
        except asyncio.TimeoutError:
            log_action(f"⏱️ Таймаут: {url}")
            return None
        except Exception as e:
            log_action(f"❌ Неизвестная ошибка: {e}")
            return None
