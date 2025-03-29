import requests
import os
import time
import logging
from tqdm import tqdm

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='download.log'
)


def download_mp4(url, output_path="downloaded_video.mp4"):
    try:
        start_time = time.time()

        # Получаем размер файла
        response = requests.head(url)
        file_size = int(response.headers.get('content-length', 0))

        if file_size == 0:
            raise ValueError("Не удалось определить размер файла")

        logging.info(f"Начало загрузки файла: {url}")
        logging.info(f"Размер файла: {file_size / 1024 / 1024:.2f} MB")

        headers = {
        }

        # Открываем файл для записи
        with open(output_path, 'wb') as file:
            with tqdm(total=file_size, unit='B', unit_scale=True, unit_divisor=1024, desc="Скачивание") as pbar:
                downloaded_size = 0
                chunk_size = 1024 * 1024  # 1 MB chunks

                while downloaded_size < file_size:
                    end_byte = min(downloaded_size + chunk_size - 1, file_size - 1)
                    headers['Range'] = f'bytes={downloaded_size}-{end_byte}'

                    response = requests.get(url, headers=headers, stream=True)
                    response.raise_for_status()

                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            size = file.write(chunk)
                            downloaded_size += size
                            pbar.update(size)

                    # Логи скорости
                    current_time = time.time()
                    elapsed = current_time - start_time
                    speed = downloaded_size / elapsed / 1024 / 1024
                    logging.info(f"Прогресс: {downloaded_size / file_size * 100:.1f}%, Скорость: {speed:.2f} MB/s")

        total_time = time.time() - start_time
        avg_speed = file_size / total_time / 1024 / 1024

        logging.info(f"Загрузка завершена! Время: {total_time:.2f} сек, Средняя скорость: {avg_speed:.2f} MB/s")
        print(f"Загрузка завершена! Файл сохранен как: {output_path}")

    except Exception as e:
        logging.error(f"Ошибка: {str(e)}")
        print(f"Произошла ошибка: {str(e)}")


if __name__ == "__main__":
    # Пример использования
    video_url = "https://rr4---sn-cxxapox31-5a5l.googlevideo.com/videoplayback?expire=1743243103&ei=_3LnZ_etK6nA0u8Po4O00Ak&ip=37.99.17.200&id=o-AG2IzHcNk61uFK1gFoqtLuv7-Fi0DhWJ7fTf5GWSURvM&itag=160&aitags=133%2C134%2C135%2C136%2C160%2C242%2C243%2C244%2C247%2C278%2C298%2C299%2C302%2C303%2C394%2C395%2C396%2C397%2C398%2C399&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&met=1743221503%2C&mh=FU&mm=31%2C29&mn=sn-cxxapox31-5a5l%2Csn-ixh7yn7d&ms=au%2Crdu&mv=m&mvi=4&pl=24&rms=au%2Cau&initcwndbps=3143750&bui=AccgBcM2tdnEwZ_rhyc60172aG4-nfNrXVWNhblHZukL7Zb48L_ckt9BV403eA2db-jTQ-uRkc7sdT51&vprv=1&svpuc=1&mime=video%2Fmp4&ns=A5Hw09UvCJoQaU14tjanzg0Q&rqh=1&gir=yes&clen=11708649&dur=1277.233&lmt=1743090758219385&mt=1743221109&fvip=3&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=4432534&n=iHNI46esFzwgrg&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRQIhALk_tluoe7PbF12RrUu8bqMP0TTWfuBO2-4Qs8X16rgYAiBc0ZoVaAGm2aubkBTkGHT_8yUuu5tz7CkstC_3pvKvVQ%3D%3D&lsparams=met%2Cmh%2Cmm%2Cmn%2Cms%2Cmv%2Cmvi%2Cpl%2Crms%2Cinitcwndbps&lsig=AFVRHeAwRQIhAId8iIZax1DP3tFivy2rVw6XDpqwrrpn2wEYVFUlK2NTAiBjeQtxeZqTykdRyVM4S4MVgbWrF3TsVdzNWq5vaYRcog%3D%3D"
    download_mp4(video_url)