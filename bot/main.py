import asyncio
from datetime import datetime, timedelta

import requests
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, handle_quality_selection
from bot.utils.YtDlpDownloader import YtDlpDownloader
from bot.utils.log import log_action
from bot.utils.video_info import check_ffmpeg_installed
from config import bot, dp, CHANNEL_IDS  # Убедитесь, что CHANNEL_IDS определен в config.py

# Временное хранилище статусов подписки
user_subscription_cache = {}

async def check_subscription(user_id: int, force_check: bool = False) -> bool:
    """
    Проверяет подписку пользователя на все требуемые каналы
    :param user_id: ID пользователя
    :param force_check: Принудительная проверка (игнорирует кеш)
    """
    try:
        # Проверяем кеш, если не принудительная проверка
        if not force_check and user_id in user_subscription_cache:
            last_check, status = user_subscription_cache[user_id]
            if datetime.now() - last_check < timedelta(minutes=10):
                return status

        # Проверяем подписку на каждый канал
        for channel_id in CHANNEL_IDS:
            chat_member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if chat_member.status not in ['member', 'administrator', 'creator']:
                user_subscription_cache[user_id] = (datetime.now(), False)
                return False

        user_subscription_cache[user_id] = (datetime.now(), True)
        return True
    except Exception as e:
        log_action("Ошибка проверки подписки", f"User {user_id}: {str(e)}")
        return False

async def send_subscription_reminder(user_id: int):
    """
    Отправляет напоминание о необходимости подписки
    """
    try:
        buttons = []
        for channel_id in CHANNEL_IDS:
            chat = await bot.get_chat(channel_id)
            invite_link = await chat.export_invite_link()
            buttons.append(
                InlineKeyboardButton(
                    text=f"Подписаться на {chat.title}",
                    url=invite_link
                )
            )

        # Добавляем кнопку "Проверить подписки"
        buttons.append(InlineKeyboardButton(
            text="✅ Проверить подписки",
            callback_data="check_subscription"
        ))

        keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])

        await bot.send_message(
            user_id,
            "📢 Для использования бота необходимо подписаться на наши каналы:",
            reply_markup=keyboard
        )
    except Exception as e:
        log_action("Ошибка отправки напоминания", f"User {user_id}: {str(e)}")

async def subscription_check_task():
    """
    Фоновая задача для периодической проверки подписок
    """
    while True:
        await asyncio.sleep(24 * 3600)  # Проверка каждые 24 часа
        log_action("Периодическая проверка подписок", "Запущено")

async def normalize_ports_for_url(url, proxy_ports, tor_manager, max_ip_attempts=10, timeout_seconds=10, required_success_ratio=0.75):
    import aiohttp
    import time
    from aiohttp_socks import ProxyConnector

    good_ports = []

    print(f"🌐 Проверка {len(proxy_ports)} Tor-портов на доступ к {url}")

    async def normalize_port(index, port):
        for attempt in range(1, max_ip_attempts + 1):
            try:
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                headers = {
                    'User-Agent': 'Mozilla/5.0',
                    'Accept': '*/*',
                    'Referer': 'https://www.youtube.com/'
                }

                async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
                    start_time = time.time()
                    async with session.head(url, allow_redirects=False) as resp:
                        elapsed = time.time() - start_time

                        if resp.status in [403, 429] or 500 <= resp.status < 600:
                            print(f"🚫 Порт {port} попытка {attempt}: Статус {resp.status}, меняю IP...")
                            await tor_manager.renew_identity(index)
                            await asyncio.sleep(2)
                            continue

                        if elapsed > 5:
                            print(f"🐌 Порт {port} попытка {attempt}: Слишком медленно ({elapsed:.2f}s), меняю IP...")
                            await tor_manager.renew_identity(index)
                            await asyncio.sleep(2)
                            continue

                        print(f"✅ Порт {port} прошёл проверку (статус {resp.status}, {elapsed:.2f}s)")
                        return port

            except Exception as e:
                print(f"❌ Порт {port} попытка {attempt}: ошибка {e}")
                await tor_manager.renew_identity(index)
                await asyncio.sleep(2)

        print(f"❌ Порт {port} не прошёл проверку после {max_ip_attempts} попыток")
        return None

    results = await asyncio.gather(*(normalize_port(i, port) for i, port in enumerate(proxy_ports)))
    good_ports = [port for port in results if port is not None]

    ratio = len(good_ports) / len(proxy_ports)
    print(f"📊 Успешные порты: {len(good_ports)} / {len(proxy_ports)} ({ratio*100:.1f}%)")

    if ratio < required_success_ratio:
        raise RuntimeError("❌ Недостаточно стабильных Tor-портов. Программа остановлена.")

    return good_ports

# Обработчик кнопки "Проверить подписки"
@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if await check_subscription(user_id, force_check=True):
        await callback.answer("✅ Вы подписаны на все каналы!", show_alert=True)
    else:
        await callback.answer("❌ Вы не подписаны на все каналы!", show_alert=True)
        await send_subscription_reminder(user_id)

# Хендлеры
@dp.message(Command("start"))
async def handle_start(message: types.Message):
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return
    await start(message)

@dp.message(F.text.startswith("http"))
async def handle_url(message: types.Message):
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return
    await handle_link(message)

@dp.message(lambda message: message.text.lower().endswith("p") or message.text.lower() == "только аудио")
async def handle_quality(message: types.Message):
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return

    # Передаем сообщение с выбором качества в handle_quality_selection
    await handle_quality_selection(message)

async def main():
    url = "https://rr4---sn-4g5lzner.googlevideo.com/videoplayback?expire=1743867093&ei=dfjwZ4HoF97yi9oPtsqH-A0&ip=185.220.101.168&id=o-AASpAOLcgfK3F93D05vleeE2CSZGOCyG5yjKipMYb196&itag=136&aitags=133,134,135,136,137,160,242,243,244,247,248,271,278,313&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&bui=AccgBcNG2dbhphLbVdTCXIs5qphhJMZm_Q-sVxBU7fO2u62UC9wq38G_sB1q2vRvyiVI941DNIKSwIEM&vprv=1&svpuc=1&mime=video/mp4&ns=24uCSg9vgQy8Nz1J8Hl5CjwQ&rqh=1&gir=yes&clen=97159747&dur=1765.430&lmt=1742103484834177&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=4432534&n=6Tj_GpT-tR_r3A&sparams=expire,ei,ip,id,aitags,source,requiressl,xpc,bui,vprv,svpuc,mime,ns,rqh,gir,clen,dur,lmt&sig=AJfQdSswRAIgcTr2-EM0iRbaadqJDNtUBQZzh6FIrSEFoLPN6LqwFskCICe4TwQLFTF6nuT3kuDQzIFJFC-tSYqtAGdvSePPczY9&rm=sn-gxuo03g-3c2l7e,sn-4g5ekr7z&rrc=79,104&fexp=24350590,24350737,24350827,24350961,24351147,24351149,24351173,24351283,24351398,24351523,24351528,24351545&req_id=64196951e7dfa3ee&rms=rdu,au&redirect_counter=2&cms_redirect=yes&cmsv=e&ipbypass=yes&met=1743845718,&mh=cr&mip=107.189.31.187&mm=29&mn=sn-4g5lzner&ms=rdu&mt=1743845342&mv=m&mvi=4&pl=24&lsparams=ipbypass,met,mh,mip,mm,mn,ms,mv,mvi,pl,rms&lsig=ACuhMU0wRAIgTMWosDMWGHGr3P7vbexh-RxjlcpiEr-JLkMih2GzBE4CICshweAZ85JzeC7ex2JxR3rVdiYmKSnwhqm-oj5kNvuG"  # твой URL
    downloader = YtDlpDownloader()
    tor_manager = downloader.tor_manager
    proxy_ports = [9050 + i * 2 for i in range(40)]

    # Проверка портов перед запуском
    log_action(f"Начало проверки пулов:")
    good_ports = await normalize_ports_for_url(url, proxy_ports, tor_manager)
    print(f"✅ Готово, стабильные порты: {good_ports}")
    asyncio.create_task(subscription_check_task())
    log_action("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


