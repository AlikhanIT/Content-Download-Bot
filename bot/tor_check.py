import yt_dlp
import pprint


def get_youtube_direct_links(url):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        'force_generic_extractor': False
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        # Отладочная печать всего словаря
        pprint.pprint(info)

        # Пробуем вытащить requested_downloads
        if 'requested_downloads' in info:
            return [d['url'] for d in info['requested_downloads'] if 'url' in d]

        # Пробуем вытащить напрямую
        if 'url' in info:
            return [info['url']]

        raise ValueError("Не удалось получить прямые ссылки на потоки")


# Запуск
if __name__ == "__main__":
    links = get_youtube_direct_links("https://www.youtube.com/watch?v=D17qqWdMY1Y")
    print("Найденные ссылки:")
    for l in links:
        print(l)
