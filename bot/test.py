import os
import time
import requests
import logging
from threading import Thread
from yt_dlp import YoutubeDL

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)

VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
FILENAME_PREFIX = 'video_copy'

def get_direct_url(video_url):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'format': '18',
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            for fmt in info['formats']:
                if fmt.get('format_id') == '18':
                    return fmt.get('url')
    except Exception as e:
        logging.error(f"Ошибка при получении ссылки: {e}")
    return None

def download_video(url, filename, index):
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length', 0))
            downloaded = 0
            with open(filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        percent = (downloaded / total) * 100
                        downloaded_mb = downloaded / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        logging.info(f'[{index}] Скачано: {percent:.2f}% ({downloaded_mb:.2f} MB / {total_mb:.2f} MB) — {filename}')
        logging.info(f'[{index}] Скачивание завершено: {filename}')
    except Exception as e:
        logging.error(f"[{index}] Ошибка при скачивании {filename}: {e}")

def main():
    direct_url = get_direct_url(VIDEO_URL)
    if not direct_url:
        logging.error("Не удалось получить прямую ссылку на видео.")
        return

    logging.info(f"Прямая ссылка на видео: {direct_url}")

    for i in range(1, 1001):
        logging.info(f"\nРаунд #{i} — Скачивание 10 файлов одновременно")

        if i < 1000:
            for j in range(1, 11):
                fname = f"{FILENAME_PREFIX}_{j}.mp4"
                if os.path.exists(fname):
                    os.remove(fname)
                    logging.debug(f"Удалён файл: {fname}")

        threads = []
        for j in range(1, 11):
            global_index = (i - 1) * 10 + j
            fname = f"{FILENAME_PREFIX}_{j}.mp4"
            t = Thread(target=download_video, args=(direct_url, fname, global_index))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        if i < 100:
            time.sleep(1)

    logging.info(f"\nEnded")



if __name__ == '__main__':
    main()
