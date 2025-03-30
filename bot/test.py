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
        for header, value in response.headers.items():
            print(f"{header}: {value}")
        file_size = int(response.headers.get('content-length', 0))

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
    video_url = "https://rr5---sn-n8v7znz7.googlevideo.com/videoplayback?expire=1743341793&ei=gfToZ9-0Mt6N6dsPt7y2GA&ip=185.220.100.243&id=o-ACYpX7KGuT0pvWVogDTAkBUb058u8N6VLicJHcwv8dzd&itag=134&aitags=133,134,135,136,160,242,243,244,247,278,298,299,302,303,308,315,394,395,396,397,398,399,400,401&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&bui=AccgBcMbvSvdsqrnPopYHx6BWnqJ92uVxQ81MPkVPI_eFPl0PY986TIj_Z1Y3IGi_RVrJdrtbnKZwwQm&vprv=1&svpuc=1&mime=video/mp4&ns=AfCl1AfG0JYCSgzeRZUDcnwQ&rqh=1&gir=yes&clen=97261418&dur=1612.640&lmt=1743272229699746&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=5532534&n=aQGo3gr7tx_z9w&sparams=expire,ei,ip,id,aitags,source,requiressl,xpc,bui,vprv,svpuc,mime,ns,rqh,gir,clen,dur,lmt&sig=AJfQdSswRAIgQnIJ7yDDT2K1nSDzcYBxo0mW7GGPZY7-PubmBTZ-DYgCIFsFLa3lwoYrIbtP5RzU6cAahd5RVJnYN3ljF-oIFa_P&rm=sn-5oxmp55u-8pxe7e,sn-4g5ekz76&rrc=79,104&fexp=24350590,24350737,24350827,24350961,24351146,24351173,24351283,24351353,24351398,24351415,24351423,24351469,24351525,24351528,24351531,24351541&req_id=7ecb041519fa3ee&rms=rdu,au&redirect_counter=2&cms_redirect=yes&cmsv=e&ipbypass=yes&met=1743320201,&mh=j1&mip=78.40.109.6&mm=29&mn=sn-n8v7znz7&ms=rdu&mt=1743319865&mv=u&mvi=5&pl=24&lsparams=ipbypass,met,mh,mip,mm,mn,ms,mv,mvi,pl,rms&lsig=AFVRHeAwRgIhAN57uYAtEztjwdAKQ9r3FlJ8ct2l4Wp8wdknfm86ckFzAiEArofe_MxmLUnSDnX4-ZUbAjuQnpIiAFHySCRtmg-1DuA%3D"
    download_mp4(video_url)