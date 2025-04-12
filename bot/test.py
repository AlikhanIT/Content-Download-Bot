import aiohttp
import asyncio

async def fetch_and_log_all_redirects(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://www.youtube.com/",
        "Range": "bytes=0-1023"
    }

    visited = set()
    step = 0

    while True:
        if url in visited:
            print(f"🔁 Обнаружена циклическая переадресация, остановка на {url}")
            return
        visited.add(url)
        step += 1

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, allow_redirects=False) as response:
                    print(f"\n🔎 Шаг {step}")
                    print(f"🟢 URL: {url}")
                    print(f"📦 Статус: {response.status}")

                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        print(f"➡️ Редирект на: {location}")
                        if not location:
                            print("⚠️ Нет заголовка Location — прерываем")
                            return
                        url = location
                        continue

                    print("\n🔐 Заголовки:")
                    for k, v in response.headers.items():
                        print(f"  {k}: {v}")

                    print("\n📄 Тело ответа (первые 1024 байта):")
                    content = await response.content.read(1024)
                    print(content[:1024])
                    return

            except Exception as e:
                print(f"❌ Ошибка при запросе к {url}: {e}")
                return

if __name__ == "__main__":
    url_to_check = "https://rr1---sn-ug5on-5a5s.googlevideo.com/videoplayback?expire=1744481143&ei=F1f6Z5H9GcTC0u8Pwf_K2QQ&ip=78.40.109.6&id=o-ACdZGdyBkAQQ0tybnP78oUkBLz8PfuetYhTlYC7dYn7t&itag=136&aitags=133%2C134%2C135%2C136%2C137%2C160%2C242%2C243%2C244%2C247%2C248%2C278%2C394%2C395%2C396%2C397%2C398%2C399&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&met=1744459543%2C&mh=XE&mm=31%2C29&mn=sn-ug5on-5a5s%2Csn-n8v7znsz&ms=au%2Crdu&mv=m&mvi=1&pl=24&rms=au%2Cau&initcwndbps=1922500&bui=AccgBcM7tJMryj_SnrD0NEg__U0pNcLmHj7H59LPnm1NiVAC_WsN6yJOCfsPmNPX1q5WjIHKtKBoytnQ&vprv=1&svpuc=1&mime=video%2Fmp4&ns=4brcs2rmMmZkMnH51cGS6EwQ&rqh=1&gir=yes&clen=32308481&dur=661.880&lmt=1743853963807915&mt=1744459021&fvip=8&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=5535534&n=hpM_VABhqboltg&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRQIhAJvPiIITUPxMFZyMHz23VM28oZePUuZZcV8LOAVw5A-gAiA_JeglmBPEdpDGSbC_xjSQWu2LcXDznOjQAnK92N4j0A%3D%3D&lsparams=met%2Cmh%2Cmm%2Cmn%2Cms%2Cmv%2Cmvi%2Cpl%2Crms%2Cinitcwndbps&lsig=ACuhMU0wRQIgIYgVz6fyRtJnIQWhpjqQNSy8V5fDkoE61Tf73r_zSuQCIQDhGobPunmbh1-bmovbxjNOHiDsJY-Z_5gh77aGK80t2g%3D%3D"  # ← Можно заменить на любой URL
    asyncio.run(fetch_and_log_all_redirects(url_to_check))
