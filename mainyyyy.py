import yt_dlp
import time

def get_fast_video_info(url):
    start_time = time.time()

    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': True,
        'simulate': True,
        'format': 'bestvideo[height<=1080]+bestaudio/best'
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        for fmt in info.get('formats', []):
            if fmt.get('height'):
                print(f"{fmt.get('format_note')}: {fmt.get('resolution')}")

    print(f"Execution time: {time.time() - start_time:.2f} seconds")

get_fast_video_info('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
