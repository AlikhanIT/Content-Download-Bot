import asyncio
from datetime import datetime, timedelta

import requests
from aiogram import types, F
from aiogram.client.session import aiohttp
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, handle_quality_selection
from bot.proxy.proxy_manager import refresh_proxies, load_proxies
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


@dp.message(Command("test"))
async def handle_test(message: types.Message):
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return

    video_url = "https://example.com/sample_video.mp4"  # Замените на реальный URL
    quality = "720p"

    await message.answer("🔄 Начинаю загрузку видео 10 раз...")

    for i in range(10):
        await message.answer(f"📥 Загружаю видео {i + 1}/10 в качестве {quality}...")

        # Создаем новый объект сообщения с обязательными параметрами и связываем с ботом
        fake_message = types.Message(
            message_id=message.message_id,
            from_user=message.from_user,
            chat=message.chat,
            text=quality,
            date=datetime.now()
        )

        # Связываем метод с ботом
        asyncio.create_task(handle_quality_selection(fake_message.as_(bot)))

    await message.answer("✅ Загрузка запущена в фоне!")


@dp.message(lambda message: message.text.lower().endswith("p") or message.text.lower() == "только аудио")
async def handle_quality(message: types.Message):
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return

    # Передаем сообщение с выбором качества в handle_quality_selection
    await handle_quality_selection(message)


def check_tor_proxy():
    proxy_url = "socks5h://127.0.0.1:9050"
    try:
        response = requests.get("http://httpbin.org/ip", proxies={"http": proxy_url, "https": proxy_url}, timeout=10)
        if response.status_code == 200:
            ip = response.json().get("origin")
            log_action("🛡 Прокси доступен", f"IP через Tor: {ip}")
            return True
    except Exception as e:
        log_action("⚠️ Прокси недоступен", str(e))
    return False


async def main():
    try:
        check_ffmpeg_installed()
    except EnvironmentError as e:
        log_action("Ошибка запуска", str(e))
        exit(1)

    await asyncio.sleep(35)
    # Проверка доступности Tor-прокси
    log_action("Проверка Tor-прокси...")
    check_tor_proxy()

    asyncio.create_task(subscription_check_task())
    log_action("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


