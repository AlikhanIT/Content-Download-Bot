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

async def check_tor_ports_with_rotation(tor_manager, proxy_ports, test_url="https://www.youtube.com/get_video_info",
                                        timeout_seconds=10, required_success_ratio=0.75, max_ip_attempts=5):
    from aiohttp_socks import ProxyConnector
    import aiohttp
    import time

    good_ports = []

    print("🔍 Проверка Tor портов с IP-ротацией...")

    async def check_and_fix_port(index, port):
        for attempt in range(max_ip_attempts):
            try:
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                headers = {'User-Agent': 'Mozilla/5.0'}

                async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
                    start_time = time.time()
                    async with session.head(test_url, allow_redirects=True) as response:
                        elapsed = time.time() - start_time

                        if response.status in [403, 429] or 500 <= response.status < 600:
                            print(f"🚫 Порт {port}, попытка {attempt+1}: статус {response.status}, пробуем сменить IP")
                            await tor_manager.renew_identity(index)
                            await asyncio.sleep(3)
                            continue

                        if elapsed > 5:
                            print(f"🐌 Порт {port}, слишком долго: {elapsed:.2f} сек, смена IP")
                            await tor_manager.renew_identity(index)
                            await asyncio.sleep(2)
                            continue

                        print(f"✅ Порт {port} работает, статус {response.status}, время {elapsed:.2f} сек")
                        return True
            except Exception as e:
                print(f"❌ Ошибка на порту {port}, попытка {attempt+1}: {e}")
                await tor_manager.renew_identity(index)
                await asyncio.sleep(2)
        print(f"❌ Порт {port} исключен после {max_ip_attempts} попыток")
        return False

    results = await asyncio.gather(*(check_and_fix_port(i, port) for i, port in enumerate(proxy_ports)))
    for i, ok in enumerate(results):
        if ok:
            good_ports.append(proxy_ports[i])

    ratio = len(good_ports) / len(proxy_ports)
    print(f"📊 Рабочих портов: {len(good_ports)}/{len(proxy_ports)} ({ratio*100:.1f}%)")

    if ratio < required_success_ratio:
        raise RuntimeError(f"❌ Слишком мало рабочих портов: {ratio*100:.1f}% < {required_success_ratio*100:.1f}%")

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
    # Проверка доступности Tor-прокси
    downloader = YtDlpDownloader()
    tor_manager = downloader.tor_manager
    proxy_ports = [9050 + i * 2 for i in range(40)]

    # Проверка портов перед запуском
    working_ports = await check_tor_ports_with_rotation(tor_manager, proxy_ports)
    asyncio.create_task(subscription_check_task())
    log_action("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


