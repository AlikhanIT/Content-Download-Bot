import aiohttp
import asyncio

async def fetch_and_log(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://www.youtube.com/",
        "Range": "bytes=0-1023"
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                print(f"\n🟢 URL: {url}")
                print(f"📦 Статус: {response.status}")

                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    print(f"➡️ Редирект на: {location}")
                    return

                print("\n🔐 Заголовки:")
                for k, v in response.headers.items():
                    print(f"  {k}: {v}")

                print("\n📄 Тело ответа (первые 1024 байта):")
                content = await response.content.read(1024)
                print(content[:1024])

        except Exception as e:
            print(f"❌ Ошибка при запросе к {url}: {e}")

if __name__ == "__main__":
    url_to_check = "https://rr1---sn-ug5on-5a5s.googlevideo.com/videoplayback?expire=1744482572&ei=rFz6Z_LlH5aG0u8P7OnF8AQ&ip=78.40.109.6&id=o-AHVDelonGR4hNhcjhhI9HdF6ykuB9BiYhkPk4qbzFsu6&itag=136&aitags=133%2C134%2C135%2C136%2C137%2C160%2C242%2C243%2C244%2C247%2C248%2C278%2C394%2C395%2C396%2C397%2C398%2C399&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&met=1744460972%2C&mh=XE&mm=31%2C29&mn=sn-ug5on-5a5s%2Csn-n8v7kn7z&ms=au%2Crdu&mv=m&mvi=1&pcm2cms=yes&pl=24&rms=au%2Cau&initcwndbps=1620000&bui=AccgBcMD0mmwbPP4gCGS0WcL8Vv2obqBHJ5O4nJ1IlXZUWZqoeQMmM6OvRw1OpOh3Bpg2OI__mE_K-no&vprv=1&svpuc=1&mime=video%2Fmp4&ns=U2A6xCdYBed1AKUyt5eXOGkQ&rqh=1&gir=yes&clen=32308481&dur=661.880&lmt=1743853963807915&mt=1744460465&fvip=7&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=5535534&n=WFChhgev8nQD3Q&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRgIhAKTInLyiuAPGumF2WR0heMpCPdXMkRq0MH49znGzkPu-AiEAyu2F1kSA4YSS0vwu5yZi3023vloQMJc4UYvnXY85e5M%3D&lsparams=met%2Cmh%2Cmm%2Cmn%2Cms%2Cmv%2Cmvi%2Cpcm2cms%2Cpl%2Crms%2Cinitcwndbps&lsig=ACuhMU0wRgIhAKMfcutl43CKIN-mB7xzcSnIQbhy4-em2xdtLfoiRiO3AiEAo7mPEcvRKZ85W7D-YzDj043YkqggIpXQSqYiT-y15hc%3D:"
    asyncio.run(fetch_and_log(url_to_check))
