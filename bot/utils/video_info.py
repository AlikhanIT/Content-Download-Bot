import yt_dlp
import requests
# Функция для получения URL превью изображения
from PIL import Image
import io

# Загрузка и оптимизация превью
async def get_thumbnail_bytes(url):
    if not isinstance(url, str):
        return None  # Проверка, что передан правильный URL

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
    # Минимальные опции для быстрой загрузки только необходимых данных
    ydl_opts = {
        'quiet': True,  # Отключаем вывод логов
        'extract_flat': True,  # Загружаем только базовые метаданные
        'force_generic_extractor': True,  # Используем стандартный извлекатель
        'noplaylist': True  # Не обрабатываем плейлисты
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Получаем только информацию без загрузки файлов
            info_dict = ydl.extract_info(url, download=False)

            # Название и идентификатор видео
            video_id = info_dict.get("id")
            title = info_dict.get("title", "Видео")
            formats = info_dict.get("formats", [])

            # Формируем карту разрешений и размеров
            resolution_sizes = {}
            max_audio_size = 0
            is_vertical_video = False

            # Сначала обрабатываем видеоформаты
            for fmt in formats:
                width = fmt.get("width")
                height = fmt.get("height")
                filesize = fmt.get("filesize")

                if width and height and filesize:
                    # Проверка, является ли видео вертикальным
                    if width < height:
                        is_vertical_video = True

                    # Преобразуем размер в мегабайты
                    filesize_mb = float(filesize) / (1024 * 1024)
                    resolution = f"{width}x{height}"
                    resolution_sizes[resolution] = max(resolution_sizes.get(resolution, 0), filesize_mb)

            # Если видео вертикальное, передаем пустой массив качеств
            if is_vertical_video:
                resolution_sizes = {}

            # Теперь находим максимальный размер аудиофайла
            for fmt in formats:
                if fmt.get("vcodec") == "none":  # Это аудиоформат
                    filesize = fmt.get("filesize")
                    if filesize:
                        filesize_mb = float(filesize) / (1024 * 1024)
                        # Обновляем максимальный размер аудио
                        max_audio_size = max(max_audio_size, filesize_mb)

            # Если найден максимальный аудиофайл, добавляем его размер ко всем видеоразрешениям
            if max_audio_size > 0:
                for resolution in resolution_sizes:
                    resolution_sizes[resolution] += max_audio_size

            # Возвращаем данные
            return video_id, title, resolution_sizes
        except Exception as e:
            return None, None, {}
