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
    video_url = "https://rr1---sn-ab5l6nrl.googlevideo.com/videoplayback?expire=1743252039&ei=55XnZ5SfO8-1kucPic3jgAU&ip=185.220.103.8&id=o-AEBD__lAxY_ud5NVnqjBRGfYN34mBUmT4ln4emPQ6Byw&itag=160&aitags=133%2C134%2C135%2C136%2C160%2C242%2C243%2C244%2C247%2C278%2C298%2C299%2C302%2C303%2C394%2C395%2C396%2C397%2C398%2C399&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&met=1743230439%2C&mh=FU&mm=31%2C29&mn=sn-ab5l6nrl%2Csn-ab5sznzd&ms=au%2Crdu&mv=m&mvi=1&pl=24&rms=au%2Cau&initcwndbps=555000&bui=AccgBcM93809r8nx6QEOCewIkX_-kn2ULPmAnK-oYIRKCFUB_FeKUUUQNHqThaCxU0bI2VwTwb10tdqi&vprv=1&svpuc=1&mime=video%2Fmp4&ns=OOJAsE5CFBRZzxHiMJ4RrYMQ&rqh=1&gir=yes&clen=11708649&dur=1277.233&lmt=1743090758219385&mt=1743229998&fvip=5&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=4432534&n=lhAn1mqPYUTR1Q&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRAIgYW9fCAyJfhooCdZ1QSb1NG86A8JwLjnD_dCSSCuKt_4CIH1vOoRIoSlovvvBXkoOr_Xd2H9j0DUXw47RLms25JgG&lsparams=met%2Cmh%2Cmm%2Cmn%2Cms%2Cmv%2Cmvi%2Cpl%2Crms%2Cinitcwndbps&lsig=AFVRHeAwRgIhALtNARboeKP7C-LBUPm-zBDilddImJn5I3gBpeaOwXOxAiEA7OSdD7eCcJIwrX1rcYs8sl3orOxFDepH_QNF_ruJeuQ%3D"
    #video_url = "https://rr1---sn-ab5l6nrl.googlevideo.com/videoplayback?expire=1743252039&ei=55XnZ5SfO8-1kucPic3jgAU&ip=185.220.103.8&id=o-AEBD__lAxY_ud5NVnqjBRGfYN34mBUmT4ln4emPQ6Byw&itag=160&aitags=133%2C134%2C135%2C136%2C160%2C242%2C243%2C244%2C247%2C278%2C298%2C299%2C302%2C303%2C394%2C395%2C396%2C397%2C398%2C399&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&met=1743230439%2C&mh=FU&mm=31%2C29&mn=sn-ab5l6nrl%2Csn-ab5sznzd&ms=au%2Crdu&mv=m&mvi=1&pl=24&rms=au%2Cau&initcwndbps=555000&bui=AccgBcM93809r8nx6QEOCewIkX_-kn2ULPmAnK-oYIRKCFUB_FeKUUUQNHqThaCxU0bI2VwTwb10tdqi&vprv=1&svpuc=1&mime=video%2Fmp4&ns=OOJAsE5CFBRZzxHiMJ4RrYMQ&rqh=1&gir=yes&clen=11708649&dur=1277.233&lmt=1743090758219385&mt=1743229998&fvip=5&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=4432534&n=lhAn1mqPYUTR1Q&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRAIgYW9fCAyJfhooCdZ1QSb1NG86A8JwLjnD_dCSSCuKt_4CIH1vOoRIoSlovvvBXkoOr_Xd2H9j0DUXw47RLms25JgG&lsparams=met%2Cmh%2Cmm%2Cmn%2Cms%2Cmv%2Cmvi%2Cpl%2Crms%2Cinitcwndbps&lsig=AFVRHeAwRgIhALtNARboeKP7C-LBUPm-zBDilddImJn5I3gBpeaOwXOxAiEA7OSdD7eCcJIwrX1rcYs8sl3orOxFDepH_QNF_ruJeuQ%3D"
    #video_url = "https://rr4---sn-cxxapox31-5a5l.googlevideo.com/videoplayback?expire=1743252039&ei=55XnZ5SfO8-1kucPic3jgAU&ip=185.220.103.8&id=o-AEBD__lAxY_ud5NVnqjBRGfYN34mBUmT4ln4emPQ6Byw&itag=160&aitags=133%2C134%2C135%2C136%2C160%2C242%2C243%2C244%2C247%2C278%2C298%2C299%2C302%2C303%2C394%2C395%2C396%2C397%2C398%2C399&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&rms=au%2Cau&bui=AccgBcM93809r8nx6QEOCewIkX_-kn2ULPmAnK-oYIRKCFUB_FeKUUUQNHqThaCxU0bI2VwTwb10tdqi&vprv=1&svpuc=1&mime=video%2Fmp4&ns=OOJAsE5CFBRZzxHiMJ4RrYMQ&rqh=1&gir=yes&clen=11708649&dur=1277.233&lmt=1743090758219385&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=4432534&n=lhAn1mqPYUTR1Q&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRAIgYW9fCAyJfhooCdZ1QSb1NG86A8JwLjnD_dCSSCuKt_4CIH1vOoRIoSlovvvBXkoOr_Xd2H9j0DUXw47RLms25JgG&redirect_counter=1&rm=sn-ab5el67e&rrc=104&fexp=24350590,24350737,24350827,24350961,24351146,24351149,24351173,24351283,24351353,24351398,24351415,24351423,24351470,24351528&req_id=93dc7210e2c9a3ee&cms_redirect=yes&cmsv=e&ipbypass=yes&met=1743230443,&mh=FU&mip=37.99.17.200&mm=31&mn=sn-cxxapox31-5a5l&ms=au&mt=1743230231&mv=m&mvi=4&pl=24&lsparams=ipbypass,met,mh,mip,mm,mn,ms,mv,mvi,pl,rms&lsig=AFVRHeAwQwIga2SkOOaivzEZkAa0SIhCL9iBwyGxLw7sNoPGu6OEQjcCHxM84VAnYluFHemnbKKYuPxZdOCdQYV8S6umGwYtiBc%3D"
    download_mp4(video_url)