import asyncio
import shutil
from urllib.parse import urlparse, parse_qs

import ffmpeg
import yt_dlp
import requests
from PIL import Image
import io

from aiogram.client.session import aiohttp

from bot.proxy.proxy_manager import get_available_proxy
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


async def get_video_info(url):
    log_action('⚡ Быстрое получение информации о видео через oEmbed')

    try:
        # Получаем video_id
        parsed_url = urlparse(url)
        video_id = None

        if 'youtube.com' in parsed_url.netloc:
            query = parse_qs(parsed_url.query)
            video_id = query.get('v', [None])[0]
        elif 'youtu.be' in parsed_url.netloc:
            video_id = parsed_url.path.strip('/')

        if not video_id:
            log_action('❌ Не удалось извлечь ID видео')
            return None, None, None

        # oEmbed запрос
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
    proxy = {'ip': '127.0.0.1', 'port': '9150', 'user': '', 'password': ''}  # Tor по умолчанию
    proxy_url = f"socks5://{proxy['ip']}:{proxy['port']}"
    log_action(f"🛡 Используется прокси: {proxy_url}")
    return {
        'url': proxy_url,
        'key': f"{proxy['ip']}:{proxy['port']}"
    }