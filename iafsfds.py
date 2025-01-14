import asyncio

from yt_dlp import YoutubeDL
from bot.utils.log import log_action


# 📦 Получаем 'clen' или размер файла из метаданных видео
async def get_clen(url):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,  # Получаем подробные форматы
        'format': 'best',  # Извлекаем лучшее качество
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            formats = info_dict.get('formats', [])

            for fmt in formats:
                # 🔎 Проверяем наличие 'filesize' или 'clen'
                clen = fmt.get('filesize') or fmt.get('filesize_approx') or fmt.get('clen')
                if clen:
                    return int(clen)  # ✅ Возвращаем размер файла в байтах

            log_action("⚠️ Не удалось найти 'clen' или 'filesize'.")
            return None

    except Exception as e:
        log_action(f"❌ Ошибка извлечения 'clen': {e}")
        return None


# Асинхронная оболочка для вызова функции
async def main():
    url = "https://www.youtube.com/watch?v=14R_P8fk4Do"
    clen = await get_clen(url)

    if clen:
        print(f"Размер видео: {clen / (1024 * 1024):.2f} MB")
    else:
        print("Не удалось получить размер видео.")


# Запуск асинхронной функции
if __name__ == "__main__":
    asyncio.run(main())
