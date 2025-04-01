import asyncio
import aiohttp
import aiofiles
import os
import time
from collections import defaultdict
from tqdm import tqdm
from aiohttp_socks import ProxyConnector

# === НАСТРОЙКИ ===
DOWNLOAD_URL = "https://rr2---sn-4g5lznes.googlevideo.com/videoplayback?expire=1743553899&ei=CzHsZ7aSBp2Xv_IPxqfOmAs&ip=185.40.4.29&id=o-AF6OTxgVldffjobiEr3CTleSR6DICRmWYXEwUaPv5YI9&itag=136&aitags=133,134,135,136,160,242,243,244,247,278,298,299,302,303,308,394,395,396,397,398,399,400&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&bui=AccgBcMBIqB3C8SkhL3JstSgzq13mLEnPcmZk_eIYoZ-nw1bwyRlDLpcVmQLcP3L2GhFep3_XJUW8w05&vprv=1&svpuc=1&mime=video/mp4&ns=bxJGVXiuAyl2rl9sv4quIgYQ&rqh=1&gir=yes&clen=278865202&dur=1555.833&lmt=1743462817111472&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=4432534&n=vGZQIrHQde_rZg&sparams=expire,ei,ip,id,aitags,source,requiressl,xpc,bui,vprv,svpuc,mime,ns,rqh,gir,clen,dur,lmt&sig=AJfQdSswRAIgZ4yREXjM9A1kNGGE-tpmvuHKJqWnH-sAa1bZv1mepCYCIAD27yN1CO7YLFMqScxPDFz2R4ZWhtGDMCFemg_jdCqk&rm=sn-i5hes7z&rrc=104,80,80&fexp=24350590,24350737,24350825,24350827,24350961,24351146,24351149,24351173,24351207,24351230,24351283,24351353,24351398,24351415,24351422,24351423,24351442,24351470,24351526,24351528,24351532,24351543&req_id=26b7dbcccff3a3ee&ipbypass=yes&cm2rm=sn-apaapm4g-apae7l,sn-25gkz7s&redirect_counter=3&cms_redirect=yes&cmsv=e&met=1743532308,&mh=BA&mip=80.67.167.81&mm=34&mn=sn-4g5lznes&ms=ltu&mt=1743531886&mv=m&mvi=2&pl=24&rms=ltu,au&lsparams=ipbypass,met,mh,mip,mm,mn,ms,mv,mvi,pl,rms&lsig=AFVRHeAwRQIgYyK-cC7qOTGEZlKSIfRmXG24JrT6E4-vb6VSq3tkPfQCIQC3qU3Vn1e6pzK4zbPbt6ohNxO4JR1XCCeAaPf1fuf68g%3D%3D"
MEDIA_TYPE = "video"  # или "audio"
PROXY_START = 9050
PROXY_COUNT = 40
PROXY_STEP = 2
PROXY_PORTS = [PROXY_START + i * PROXY_STEP for i in range(PROXY_COUNT)]
THREADS = 64
DOWNLOADS = 3  # количество одновременных загрузок


def log_action(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


async def download_direct(url, filename, media_type, proxy_ports, num_parts, stats):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': '*/*',
            'Referer': 'https://www.youtube.com/'
        }

        timeout = aiohttp.ClientTimeout(total=60)
        current_url = url
        total = 0
        banned_ports = {}
        port_403_counts = defaultdict(int)

        while True:
            for port in proxy_ports:
                if banned_ports.get(port, 0) > time.time():
                    continue
                try:
                    connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
                        async with session.head(current_url, allow_redirects=True) as r:
                            if r.status in (403, 429):
                                port_403_counts[port] += 1
                                if port_403_counts[port] >= 5:
                                    banned_ports[port] = time.time() + 600
                                    log_action(f"🚫 Порт {port} забанен на 10 мин")
                                continue
                            r.raise_for_status()
                            total = int(r.headers.get('Content-Length', 0))
                            break
                    if total > 0:
                        break
                except Exception as e:
                    log_action(f"⚠️ HEAD ошибка с портом {port}: {e}")
                    continue
            else:
                await asyncio.sleep(1)
                continue
            break

        log_action(f"⬇️ Размер файла: {total / (1024 * 1024):.2f} MB")
        part_size = total // num_parts
        ranges = [(i * part_size, min((i + 1) * part_size - 1, total - 1)) for i in range(num_parts)]
        remaining = set(range(len(ranges)))

        pbar = tqdm(total=total, unit='B', unit_scale=True, desc=filename)
        start_time = time.time()

        sessions = {}
        for port in proxy_ports:
            connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
            sessions[port] = aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector)

        semaphore = asyncio.Semaphore(24)

        async def download_range(index):
            start, end = ranges[index]
            part_file = f"{filename}.part{index}"
            for attempt in range(20):
                available_ports = [p for p in proxy_ports if banned_ports.get(p, 0) < time.time()]
                if not available_ports:
                    raise Exception("❌ Нет доступных портов")
                port = available_ports[(index + attempt) % len(available_ports)]
                session = sessions.get(port)
                if not session or session.closed:
                    await asyncio.sleep(1)
                    continue
                try:
                    range_headers = headers.copy()
                    range_headers['Range'] = f'bytes={start}-{end}'
                    async with semaphore:
                        async with session.get(current_url, headers=range_headers) as resp:
                            if resp.status in (403, 429, 409):
                                port_403_counts[port] += 1
                                if port_403_counts[port] >= 5:
                                    banned_ports[port] = time.time() + 600
                                continue
                            resp.raise_for_status()
                            async with aiofiles.open(part_file, 'wb') as f:
                                async for chunk in resp.content.iter_chunked(1024 * 1024):
                                    await f.write(chunk)
                                    pbar.update(len(chunk))
                    remaining.discard(index)
                    return
                except Exception:
                    await asyncio.sleep(2)
                    continue
            raise Exception(f"❌ Провал диапазона {index}")

        await asyncio.gather(*(download_range(i) for i in range(len(ranges))))
        for session in sessions.values():
            await session.close()
        pbar.close()

        async with aiofiles.open(filename, 'wb') as out:
            for i in range(len(ranges)):
                part = f"{filename}.part{i}"
                async with aiofiles.open(part, 'rb') as pf:
                    while chunk := await pf.read(1024 * 1024):
                        await out.write(chunk)
                os.remove(part)

        elapsed = time.time() - start_time
        log_action(f"✅ Скачано {filename} за {elapsed:.2f} сек")
        stats.append((filename, elapsed))

    except Exception as e:
        log_action(f"❌ Ошибка: {e}")
        stats.append((filename, -1))


async def run_multiple():
    await asyncio.sleep(80)
    stats = []
    tasks = []
    for i in range(DOWNLOADS):
        fname = f"output_{i + 1}.mp4"
        tasks.append(download_direct(DOWNLOAD_URL, fname, MEDIA_TYPE, PROXY_PORTS, THREADS, stats))
    await asyncio.gather(*tasks)
    print("\n📊 Итоговая статистика:")
    for fname, duration in stats:
        if duration > 0:
            print(f"{fname}: {duration:.2f} сек")
        else:
            print(f"{fname}: ❌ Ошибка")


if __name__ == "__main__":
    asyncio.run(run_multiple())