import asyncio
import traceback
from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from bot.config import bot
from bot.utils.downloader import download_and_send
from bot.utils.log import log_action
from bot.utils.video_info import get_video_resolutions_and_sizes

current_links = {}
downloading_status = {}

async def handle_link(message: types.Message, use_dynamic_qualities: bool = False):
    user = message.from_user
    text = message.text.strip()

    log_action("Ссылка от пользователя", f"Пользователь: {user.id} ({user.username}), Ссылка: {text}")

    size_map = await get_video_resolutions_and_sizes(text) if use_dynamic_qualities else {}

    # Соответствие разрешений и качества
    resolution_to_quality = {
        '256x144': '144p',
        '426x240': '240p',
        '640x360': '360p',
        '854x480': '480p',
        '1280x720': '720p',
        '1920x1080': '1080p',
        '2560x1440': '1440p',
        '3840x2160': '2160p'
    }

    predefined_quality_order = ["144p", "360p", "720p"]

    keyboard_buttons = []

    if not use_dynamic_qualities or not size_map:
        # Используем предустановленные качества
        for quality in predefined_quality_order:
            keyboard_buttons.append(KeyboardButton(text=f"{quality}"))
    else:
        for resolution, size in size_map.items():
            quality = resolution_to_quality.get(resolution)
            if quality and quality in predefined_quality_order:
                size_mb = round(size, 1)
                keyboard_buttons.append(KeyboardButton(text=f"{quality} ({size_mb} MB)"))

        # Сортировка по порядку качества
        keyboard_buttons = sorted(keyboard_buttons, key=lambda button: predefined_quality_order.index(button.text.split()[0]))

    keyboard_buttons.append(KeyboardButton(text="Только аудио"))
    keyboard = ReplyKeyboardMarkup(
        keyboard=[keyboard_buttons[i:i + 2] for i in range(0, len(keyboard_buttons), 2)],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    current_links[user.id] = text
    await message.answer("Выберите качество или только аудио:", reply_markup=keyboard)


async def handle_quality_selection(message: types.Message):
    user = message.from_user
    text = message.text.strip()

    if downloading_status.get(user.id):
        await message.answer("Видео уже скачивается. Пожалуйста, подождите.")
        return

    if user.id not in current_links:
        await message.answer("Сначала отправьте ссылку на видео.")
        return

    url = current_links.pop(user.id)

    if text == "Только аудио":
        quality = "0"
        download_type = "audio"
    else:
        quality = text.split(" ")[0].replace("p", "")
        download_type = "video"

    await message.answer("🔄 Начался процесс скачивания...")
    downloading_status[user.id] = True

    # Запускаем скачивание в фоне, чтобы не блокировать работу бота
    asyncio.create_task(download_and_send_wrapper(user.id, url, download_type, quality))



async def download_and_send_wrapper(user_id, url, download_type, quality):
    """
    Запускает скачивание видео в фоне.
    """
    try:
        await download_and_send(user_id, url, download_type, quality)
    except Exception as e:
        error_trace = traceback.format_exc()
        log_action(f"Ошибка при скачивании: {error_trace}")
        current_links.pop(user_id, None)  # безопасно удаляет, если ключ есть
        await bot.send_message(user_id, f"❌ Произошла ошибка при скачивании: {e}")
    finally:
        downloading_status.pop(user_id, None)
