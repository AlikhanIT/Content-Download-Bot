import asyncio
import json
from bot.utils.log import log_action
import subprocess


def determine_quality(width, height):
    if height is not None:  # Если высота доступна, используем её
        if height <= 144:
            return "144p"
        elif height <= 360:
            return "360p"
        elif height <= 720:
            return "720p"
        elif height <= 1080:
            return "1080p"
    elif width is not None:  # Если только ширина доступна
        if width <= 256:
            return "144p"
        elif width <= 426:
            return "360p"
        elif width <= 854:
            return "720p"
        elif width <= 1920:
            return "1080p"
    return None  # Если нет ни высоты, ни ширины


async def get_video_info(url):
    command = ["yt-dlp", "--dump-json", url]
    process = await asyncio.create_subprocess_exec(*command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = await process.communicate()

    if process.returncode == 0:
        data = json.loads(stdout.decode())
        video_id = data.get("id")
        title = data.get("title", "Видео")
        formats = data.get("formats", [])
        size_map = {}
        all_filesizes = []

        for fmt in formats:
            width = fmt.get("width")
            height = fmt.get("height")
            filesize = fmt.get("filesize")

            # Преобразуем `filesize` в float и увеличиваем на 1.5
            try:
                filesize = float(filesize) * 1.5 if filesize else 0.0
            except ValueError:
                filesize = 0.0

            # Сохраняем все размеры для вычисления минимального
            if filesize > 0:
                all_filesizes.append(filesize)

            # Определяем качество
            quality = determine_quality(width, height)

            if quality:
                # Если качество уже есть, используем максимальный размер
                if quality in size_map:
                    size_map[quality] = max(size_map.get(quality, 0.0), filesize)
                else:
                    size_map[quality] = filesize

        # Если размеры недоступны, определяем минимальный доступный размер больше 0
        min_filesize = min(all_filesizes) if all_filesizes else 0.0

        # Присваиваем минимальный размер отсутствующим качествам
        for quality in ["144p", "360p", "720p", "1080p"]:
            if quality not in size_map:
                size_map[quality] = min_filesize

        # Упорядочиваем размеры по качеству
        ordered_qualities = ["144p", "360p", "720p", "1080p"]
        sorted_size_map = {
            q: size_map[q]  # Возвращаем размер в float
            for q in ordered_qualities if q in size_map
        }

        return video_id, title, sorted_size_map
    else:
        log_action("Ошибка получения информации о видео", stderr.decode())
        return None, None, {}
