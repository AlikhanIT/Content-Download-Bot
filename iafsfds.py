from yt_dlp import YoutubeDL

def get_itag_list(url):
    ydl_opts = {
        'quiet': True,  # Отключаем логи для чистоты вывода
        'skip_download': True,  # Не скачивать видео, только информацию
    }

    with YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)
        formats = info_dict.get('formats', [])

        print(f"\nДоступные форматы для видео: {info_dict.get('title')}\n{'='*50}")

        for fmt in formats:
            itag = fmt.get('format_id')
            ext = fmt.get('ext')
            resolution = fmt.get('resolution') or f"{fmt.get('width')}x{fmt.get('height')}"
            filesize = fmt.get('filesize') or 0
            filesize_mb = round(filesize / (1024 * 1024), 2) if filesize else "N/A"
            fps = fmt.get('fps') or "N/A"
            vcodec = fmt.get('vcodec')
            acodec = fmt.get('acodec')

            print(f"itag: {itag} | {ext.upper()} | {resolution} | {filesize_mb} MB | {fps} FPS | Video Codec: {vcodec} | Audio Codec: {acodec}")

# Пример вызова функции
video_url = 'https://www.youtube.com/watch?v=8LoJTmd37Y4'
get_itag_list(video_url)
