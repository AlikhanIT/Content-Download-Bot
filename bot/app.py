# main.py

import asyncio
import os
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
    test_url = "https://rr3---sn-ixh7yn7e.googlevideo.com/videoplayback?expire=1748479756&ei=rFo3aInZCsniv_IP6e7CoQU&ip=37.99.46.151&id=o-AN6GxMe7GrVqbxx6p0tYizNfJ4oZ5wc4CvKt4EtsPt56&itag=398&aitags=133%2C134%2C135%2C136%2C160%2C242%2C243%2C244%2C247%2C278%2C298%2C299%2C302%2C303%2C308%2C394%2C395%2C396%2C397%2C398%2C399%2C400&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&bui=AY1jyLMAg7pH7dLXTsaFujWiHr_Wcfn1kcP0buvY8u_-7u-MZz2Qhmw4RE-a1MkfrdNDo1crirTtuKQE&vprv=1&svpuc=1&mime=video%2Fmp4&ns=RZ9dD-8mQLzs08-WM0_LvyAQ&rqh=1&gir=yes&clen=858643172&dur=3868.633&lmt=1682264004898307&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=5537434&n=rVr1Dr9O6h1vFw&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRAIgcpqcsKZuxfyFQhI5IpeWuObMsmX264c_ABjN7AoFc9cCIB50SUBgPena4Bh68E7exBt2DD_c1bByzt8ykjeOgJ5E&redirect_counter=1&cm2rm=sn-cxxapox31-5a567l&rrc=80&fexp=24350590,24350737,24350827,24350961,24351173,24351495,24351528,24351594,24351638,24351658,24351661,24351759,24351790,24351864,24351907,24352018,24352020&req_id=20ac18e4d3faa3ee&cms_redirect=yes&cmsv=e&met=1748458177,&mh=iA&mm=29&mn=sn-ixh7yn7e&ms=rdu&mt=1748456727&mv=m&mvi=3&pl=23&rms=rdu,au&lsparams=met,mh,mm,mn,ms,mv,mvi,pl,rms&lsig=APaTxxMwRQIgEgP49MGqYYG6cQTHKdM-9IYg2eSmEvxIuK6t34qLYYICIQCQ6gY67GKC9Om2NqScvyV2i4ewOdhFnXzgmh5NTE4J5g%3D%3D"

    # Запуск фоновой задачи по разбану портов
    asyncio.create_task(unban_ports_forever(test_url))
    proxy_ports = [9050 + i * 2 for i in range(40)]
    # Пример: для одного URL, или позже подставляй динамически
    asyncio.create_task(normalize_all_ports_forever_for_url(test_url, proxy_ports))
    # asyncio.create_task(initialize_all_ports_once(test_url, proxy_ports))

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
            log_action(f"❌ Ошибка загрузки [{task_id}]: {e}")
            download_status[task_id].update({
                "status": "error",
                "error": str(e)
            })

# 🚀 Запуск сервера прямо в Python
def main():
    config = Config(app=app, host="0.0.0.0", port=8000, reload=False)
    server = Server(config)
    asyncio.run(server.serve())

if __name__ == "__main__":
    main()
