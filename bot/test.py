import os
import time
import requests
from threading import Thread
from yt_dlp import YoutubeDL

VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
FILENAME_PREFIX = 'video_copy'

def get_direct_url(video_url):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'format': '18',
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        for fmt in info['formats']:
            if fmt.get('format_id') == '18':
                return fmt.get('url')
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
                        print(f'\r[{index}] Скачано: {percent:.2f}% ({downloaded_mb:.2f} MB / {total_mb:.2f} MB) — {filename}', end='', flush=True)
        print(f'\n[{index}] Скачивание завершено: {filename}')
    except Exception as e:
        print(f"\n[{index}] Ошибка при скачивании {filename}: {e}")

def main():
    direct_url = get_direct_url(VIDEO_URL)
    if not direct_url:
        print("Не удалось получить прямую ссылку на видео.")
        return

    print(f"Прямая ссылка на видео: {direct_url}\n")

    for i in range(1, 1001):
        print(f"\nРаунд #{i} — Скачивание 10 файлов одновременно")

        # Удаляем старые файлы (кроме последнего раунда)
        if i < 1000:
            for j in range(1, 11):
                fname = f"{FILENAME_PREFIX}_{j}.mp4"
                if os.path.exists(fname):
                    os.remove(fname)

        threads = []
        for j in range(1, 11):
            fname = f"{FILENAME_PREFIX}_{j}.mp4"
            t = Thread(target=download_video, args=(direct_url, fname, j))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        if i < 100:
            time.sleep(1)

if __name__ == '__main__':
    main()
