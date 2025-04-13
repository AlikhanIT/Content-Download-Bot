import asyncio
from datetime import datetime, timedelta

import requests
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, download_and_send_wrapper, current_links, downloading_status
from bot.utils.YtDlpDownloader import YtDlpDownloader
from bot.utils.log import log_action
from bot.utils.tor_port_manager import normalize_all_ports_forever_for_url, unban_ports_forever
from bot.utils.video_info import check_ffmpeg_installed, get_video_info_with_cache, extract_url_from_info, \
    resolve_final_url
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

@dp.callback_query(lambda c: c.data.startswith("quality_"))
async def video_quality_callback(callback_query: types.CallbackQuery):
    if not await check_subscription(callback_query.from_user.id, force_check=True):
        await send_subscription_reminder(callback_query.from_user.id)
        return

    data = callback_query.data.replace("quality_", "")
    if data == "audio":
        download_type = "audio"
        quality = "0"
    else:
        download_type = "video"
        quality = data.replace("p", "")

    url = current_links.pop(callback_query.from_user.id, None)
    if not url:
        await callback_query.message.edit_text("❌ Истекло время выбора или ссылка потеряна. Отправьте заново.")
        return

    asyncio.create_task(download_and_send_wrapper(
        user_id=callback_query.from_user.id,
        url=url,
        download_type=download_type,
        quality=quality
    ))

@dp.callback_query(lambda c: c.data == "cancel")
async def cancel_download(call: CallbackQuery):
    user_id = call.from_user.id
    downloading_status[user_id] = "cancelled"
    await call.message.edit_text("🚫 Загрузка отменена.")

async def main():
    await asyncio.sleep(60)
    yt_url = "https://www.youtube.com/watch?v=-uzC0K3ku5g"
    info = await get_video_info_with_cache(yt_url)
    #direct_url = await resolve_final_url(await extract_url_from_info(info, ["136"]))
    direct_url = "https://nsf-m4c-one-fr-02.sf-converter.com/prod-new/download/eyJtZWRpYUlkIjoiTk5UZl9ZTXFGYWsiLCJ0aXRsZSI6ItCf0LXRgNC10LPQvtCy0L7RgNGLINCh0KjQkCDRgSDQn9GD0YLQuNC90YvQvCB8INCj0LTQsNGA0Ysg0JLQoSDQoNCkINC_0L4g0JHQtdC70LPQvtGA0L7QtNGB0LrQvtC5INC-0LHQu9Cw0YHRgtC4IHwg0J_QvtC00YDQvtCx0L3QsNGPINC60LDRgNGC0LAg0YEg0YTRgNC-0L3RgtC-0LIgKEVuZyBzdWIpIiwiZm9ybWF0IjoibXA0IiwicXVhbGl0eSI6IjcyMCIsInRpbWVzdGFtcCI6MTc0NDU0NzU3NH0.977044d994e498197ca41ce627a8c1c5"
    log_action(direct_url)

    proxy_ports = [9050 + i * 2 for i in range(40)]

    log_action("Начало проверки пулов:")
    await asyncio.sleep(60)

    await normalize_all_ports_forever_for_url(direct_url, proxy_ports)

    await unban_ports_forever(url=direct_url, parallel=True)

    asyncio.create_task(subscription_check_task())  # Только 1 раз!

    log_action("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


