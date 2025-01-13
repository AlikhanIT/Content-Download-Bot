import yt_dlp
import requests
# Функция для получения URL превью изображения
from PIL import Image
import io
from yt_dlp import YoutubeDL
from bot.utils.log import log_action

def add_range_to_url(stream_url, clen):
    return f"{stream_url}&range=0-{clen}"

# 📦 Получаем 'clen' из метаданных видео
async def get_clen(self, url):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info_dict = ydl.extract_info(url, download=False)
            formats = info_dict.get('formats', [])
            for fmt in formats:
                if 'clen' in fmt:
                    return fmt['clen']  # ✅ Возвращаем размер
        except Exception as e:
            log_action(f"❌ Ошибка извлечения 'clen': {e}")
            return None

async def get_video_resolutions_and_sizes(url):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': True,
        'simulate': True,
        'socket_timeout': 120,
        'format': 'bestvideo[height<=1080]+bestaudio/best'
    }

    resolution_sizes = {}
    max_audio_size = 0
    is_vertical_video = False

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)
        formats = info_dict.get("formats", [])

        for fmt in formats:
            width = fmt.get("width")
            height = fmt.get("height")
            filesize = fmt.get("filesize")

            if width and height and filesize:
                if width < height:
                    is_vertical_video = True

                filesize_mb = float(filesize) / (1024 * 1024)
                resolution = f"{width}x{height}"
                resolution_sizes[resolution] = max(resolution_sizes.get(resolution, 0), filesize_mb)

        if is_vertical_video:
            resolution_sizes = {}

        for fmt in formats:
            if fmt.get("vcodec") == "none":
                filesize = fmt.get("filesize")
                if filesize:
                    filesize_mb = float(filesize) / (1024 * 1024)
                    max_audio_size = max(max_audio_size, filesize_mb)

        if max_audio_size > 0:
            for resolution in resolution_sizes:
                resolution_sizes[resolution] += max_audio_size

    return resolution_sizes

# Загрузка и оптимизация превью
async def get_thumbnail_bytes(url):
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

async def get_video_info(url):
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'force_generic_extractor': True,
        'socket_timeout': 120,
        'noplaylist': True
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info_dict = ydl.extract_info(url, download=False)

            video_id = info_dict.get("id")
            title = info_dict.get("title", "Видео")
            thumbnail_url = info_dict.get("thumbnail")  # Получаем превью

            # Возвращаем превью как четвёртый параметр
            return video_id, title, thumbnail_url

        except Exception as e:
            return None, None, {}, None
