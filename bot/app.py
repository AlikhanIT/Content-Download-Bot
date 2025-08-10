# main.py

import asyncio
import os
import traceback
import uuid
from typing import Literal, Dict

from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel, Field
from bot.utils.YtDlpDownloader import YtDlpDownloader
from bot.database.mongo import get_from_cache, save_to_cache
from bot.utils.tor_port_manager import unban_ports_forever, normalize_all_ports_forever_for_url, \
    initialize_all_ports_once
from bot.utils.video_info import get_video_info
from bot.utils.log import log_action

from uvicorn import Config, Server

app = FastAPI(title="Yt API", version="1.0")

# Конфигурация
MAX_CONCURRENT_DOWNLOADS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
downloader = YtDlpDownloader(max_threads=MAX_CONCURRENT_DOWNLOADS)
download_status: Dict[str, Dict] = {}

class DownloadRequest(BaseModel):
    url: str = Field(..., example="https://youtu.be/abc123", description="Ссылка на видео")
    media_type: Literal["video", "audio"] = Field("video", description="Тип контента")
    quality: str = Field("480", description="Качество (только для видео)")

@app.on_event("startup")
async def startup_tasks():
    test_url = "https://rr2---sn-cxxapox31-5a56.googlevideo.com/videoplayback?expire=1749395991&ei=t1VFaO2OHJqRv_IPrfeJuAE&ip=37.99.2.242&id=o-AAxzBaIuSq-PEVeGP4EBO1pVXouXEVsjarWBEClqCvto&itag=398&aitags=133%2C134%2C135%2C136%2C160%2C242%2C243%2C244%2C247%2C278%2C298%2C299%2C302%2C303%2C308%2C394%2C395%2C396%2C397%2C398%2C399%2C400&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&met=1749374391%2C&mh=iA&mm=31%2C29&mn=sn-cxxapox31-5a56%2Csn-ixh7yn7d&ms=au%2Crdu&mv=m&mvi=2&pl=24&rms=au%2Cau&initcwndbps=2728750&bui=AY1jyLNzaOQ5j1XoP09c4WaFc_aDZFFNIGuCQlWqecT3oZplLF16-GkbVokVjviHaEqc7p60WrwDyQh3&vprv=1&svpuc=1&mime=video%2Fmp4&ns=fd2z86F_3PHsAn0n-AKQ14cQ&rqh=1&gir=yes&clen=858643172&dur=3868.633&lmt=1682264004898307&mt=1749374014&fvip=2&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=5537434&n=-opRSr5zBRiBWA&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRAIgHchYb-1cLQGyWWOeDzHH9w5tASh96UJnTp2JdQSHNWQCICX_2YbHxsh7-nETI3AVrJDNVGbIzjwWdqX3pc9fNCU9&lsparams=met%2Cmh%2Cmm%2Cmn%2Cms%2Cmv%2Cmvi%2Cpl%2Crms%2Cinitcwndbps&lsig=APaTxxMwRAIgJDOO5fdBGKsaJGZ8b98ve7cQlvrbeEZaGgvHzVosSEUCIC5iM1rqjMlpIRW4ooo-sMXh6NIiBwI2Nc0h1Z5yMFWy"

    # Запуск фоновой задачи по разбану портов
    # asyncio.create_task(unban_ports_forever(test_url))
    proxy_ports = [9050]
    # Пример: для одного URL, или позже подставляй динамически
    # asyncio.create_task(normalize_all_ports_forever_for_url(test_url, proxy_ports))
    asyncio.create_task(initialize_all_ports_once(test_url, proxy_ports))

@app.post("/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    download_status[task_id] = {
        "status": "pending",
        "file_path": None,
        "error": None
    }

    background_tasks.add_task(handle_download_task, req, task_id)
    return {"task_id": task_id, "status": "started"}

@app.get("/download/status/{task_id}")
async def get_download_status(task_id: str):
    return download_status.get(task_id, {"status": "not_found", "message": "Invalid task ID"})

async def handle_download_task(req: DownloadRequest, task_id: str):
    async with semaphore:
        try:
            log_action(f"📥 API запрос: {req.url} [{req.media_type}/{req.quality}]")
            video_id, title, _ = await get_video_info(req.url)
            if not video_id:
                raise Exception("Не удалось извлечь ID видео")

            cached_file_id = await get_from_cache(video_id, req.media_type, req.quality)
            if cached_file_id:
                download_status[task_id].update({
                    "status": "cached",
                    "file_path": f"cached://{cached_file_id}"
                })
                return

            result_path = await downloader.download(
                url=req.url,
                download_type=req.media_type,
                quality=req.quality
            )

            if not os.path.exists(result_path):
                raise Exception("Файл не был создан")

            await save_to_cache(video_id, req.media_type, req.quality, result_path)
            download_status[task_id].update({
                "status": "completed",
                "file_path": result_path
            })
            log_action(f"✅ Завершено: {result_path}")
        except Exception as e:
            # Получаем информацию о месте ошибки
            tb = traceback.format_exc()
            error_line = traceback.extract_tb(e.__traceback__)[-1]

            log_action(f"❌ Ошибка загрузки [{task_id}]: {e}")
            log_action(f"📍 Файл: {error_line.filename}, строка {error_line.lineno}: {error_line.line}")
            log_action(f"📋 Полный traceback:\n{tb}")

            download_status[task_id].update({
                "status": "error",
                "error": str(e),
                "traceback": tb,
                "error_location": f"{error_line.filename}:{error_line.lineno}"
            })


# 🚀 Запуск сервера прямо в Python
def main():
    config = Config(app=app, host="0.0.0.0", port=8000, reload=False)
    server = Server(config)
    asyncio.run(server.serve())

if __name__ == "__main__":
    main()
