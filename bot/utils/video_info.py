import subprocess
import json
import requests
from PIL import Image
import io
from bot.utils.log import log_action


# 📦 Добавление range в URL для ускорения загрузки
def add_range_to_url(stream_url, clen):
    return f"{stream_url}&range=0-{clen}"


# 📦 Получаем 'clen' или размер файла из метаданных видео через системный yt-dlp
async def get_clen(url):
    try:
        command = [
            "yt-dlp",
            "-j",  # Вывод в формате JSON
            "--skip-download",
            url
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        info_dict = json.loads(result.stdout)

        formats = info_dict.get('formats', [])
        for fmt in formats:
            clen = fmt.get('filesize') or fmt.get('filesize_approx') or fmt.get('clen')
            if clen:
                return int(clen)  # ✅ Возвращаем размер файла

        log_action("⚠️ Не удалось найти 'clen' или 'filesize'.")
        return None

    except Exception as e:
        log_action(f"❌ Ошибка извлечения 'clen': {e}")
        return None


# 📹 Получаем доступные разрешения и размеры видео
async def get_video_resolutions_and_sizes(url):
    try:
        command = [
            "yt-dlp",
            "-j",
            "--skip-download",
            url
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        info_dict = json.loads(result.stdout)

        formats = info_dict.get("formats", [])
        resolution_sizes = {}
        max_audio_size = 0
        is_vertical_video = False

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
    try:
        command = [
            "yt-dlp",
            "-j",
            "--skip-download",
            url
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        info_dict = json.loads(result.stdout)

        video_id = info_dict.get("id")
        title = info_dict.get("title", "Видео")
        thumbnail_url = info_dict.get("thumbnail")

        return video_id, title, thumbnail_url

    except Exception as e:
        log_action(f"❌ Ошибка получения информации о видео: {e}")
        return None, None, None
