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
from bot.utils.video_info import check_ffmpeg_installed, get_video_info_with_cache, extract_url_from_info
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

async def normalize_all_ports_forever_for_url(
    url,
    proxy_ports,
    tor_manager,
    timeout_seconds=5,
    max_acceptable_response_time=5.0,
    min_speed_kbps=300
):
    import aiohttp
    import time
    from aiohttp_socks import ProxyConnector

    port_speed_log = {}

    print(f"\n🔁 Бесконечная проверка {len(proxy_ports)} Tor-портов на доступ к: {url}\n")

    async def normalize_port_forever(index, port):
        attempt = 0
        while True:
            attempt += 1
            try:
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                headers = {
                    'User-Agent': 'Mozilla/5.0',
                    'Accept': '*/*',
                    'Referer': 'https://www.youtube.com/'
                }

                print(f"[{port}] 🧪 Попытка #{attempt} — HEAD-запрос...")

                async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
                    start_time = time.time()
                    async with session.head(url, allow_redirects=False) as resp:
                        elapsed = time.time() - start_time
                        content_length = resp.headers.get("Content-Length")

                        # 💥 Блокировка или бан
                        if resp.status in [403, 429]:
                            print(f"[{port}] 🚫 Статус {resp.status} — IP забанен ({elapsed:.2f}s)")
                            await tor_manager.renew_identity(index)
                            print(f"[{port}] 🔄 IP сменён → повтор HEAD-запроса")
                            await asyncio.sleep(2)
                            continue

                        # 💥 Ошибки сервера
                        if 500 <= resp.status < 600:
                            print(f"[{port}] ❌ Серверная ошибка {resp.status}")
                            await tor_manager.renew_identity(index)
                            print(f"[{port}] 🔄 IP сменён → повтор HEAD-запроса")
                            await asyncio.sleep(2)
                            continue

                        # 🐢 Слишком медленно по времени
                        if elapsed > max_acceptable_response_time:
                            print(f"[{port}] 🐢 Медленно: {elapsed:.2f}s > {max_acceptable_response_time}s")
                            await tor_manager.renew_identity(index)
                            print(f"[{port}] 🔄 IP сменён → повтор HEAD-запроса")
                            await asyncio.sleep(2)
                            continue

                        # 🐢 Слишком медленно по скорости
                        if content_length:
                            try:
                                content_length_bytes = int(content_length)
                                speed_kbps = (content_length_bytes / 1024) / elapsed
                                if speed_kbps < min_speed_kbps:
                                    print(f"[{port}] 🐌 Низкая скорость: {speed_kbps:.2f} KB/s < {min_speed_kbps} KB/s")
                                    await tor_manager.renew_identity(index)
                                    print(f"[{port}] 🔄 IP сменён → повтор HEAD-запроса")
                                    await asyncio.sleep(2)
                                    continue
                            except Exception:
                                pass

                        # ✅ Всё хорошо!
                        print(f"[{port}] ✅ Успех! Статус {resp.status} | Время: {elapsed:.2f}s | Попытка #{attempt}")
                        port_speed_log[port] = elapsed
                        return

            except Exception as e:
                print(f"[{port}] ❌ Ошибка: {e} | Попытка #{attempt}")
                await tor_manager.renew_identity(index)
                print(f"[{port}] 🔄 IP сменён после ошибки → повтор HEAD-запроса")
                await asyncio.sleep(2)

    await asyncio.gather(*(normalize_port_forever(i, port) for i, port in enumerate(proxy_ports)))

    print("\n📈 Финальный отчёт по HEAD-запросам:")
    for port in sorted(port_speed_log.keys()):
        print(f"✅ Порт {port}: {port_speed_log[port]:.2f} сек")

    return list(port_speed_log.keys()), port_speed_log


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
    yt_url = "https://www.youtube.com/watch?v=-uzC0K3ku5g"  # 🔁 Здесь вставь свою ютуб-ссылку
    downloader = YtDlpDownloader()
    info = await get_video_info_with_cache(yt_url)
    direct_url = await extract_url_from_info(info, ["136"])

    tor_manager = downloader.tor_manager
    proxy_ports = [9050 + i * 2 for i in range(40)]

    log_action(f"Начало проверки пулов:")
    await asyncio.sleep(60)
    good_ports = await normalize_all_ports_forever_for_url(direct_url, proxy_ports, tor_manager)
    print(f"✅ Готово, стабильные порты: {good_ports}")
    asyncio.create_task(subscription_check_task())
    log_action("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


