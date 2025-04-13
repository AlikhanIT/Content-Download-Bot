import asyncio
import traceback
from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from bot.config import bot
from bot.utils.downloader import download_and_send
from bot.utils.log import log_action
from bot.utils.video_info import get_video_resolutions_and_sizes, get_video_info_with_cache

current_links = {}
downloading_status = {}
progress_messages = {}

# 📥 Сюда складываем последние сообщения с кнопками, чтобы потом удалять
sent_quality_messages = {}

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


def build_inline_keyboard(qualities):
    buttons = []
    for q in qualities:
        buttons.append(InlineKeyboardButton(text=q, callback_data=f"quality:{q}"))
    buttons.append(InlineKeyboardButton(text="🎧 Только аудио", callback_data="quality:audio"))
    # Разбиваем на строки по 2 кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)])
    return keyboard



async def handle_link(message: types.Message, use_dynamic_qualities: bool = False):
    user = message.from_user
    text = message.text.strip()

    log_action("Ссылка от пользователя", f"Пользователь: {user.id} ({user.username}), Ссылка: {text}")
    asyncio.create_task(get_video_info_with_cache(text))

    size_map = await get_video_resolutions_and_sizes(text) if use_dynamic_qualities else {}
    resolution_to_quality = {
        '256x144': '144p', '426x240': '240p', '640x360': '360p',
        '854x480': '480p', '1280x720': '720p', '1920x1080': '1080p',
        '2560x1440': '1440p', '3840x2160': '2160p'
    }
    predefined_quality_order = ["144p", "360p", "720p"]

    buttons = []
    if not use_dynamic_qualities or not size_map:
        for q in predefined_quality_order:
            buttons.append(InlineKeyboardButton(text=q, callback_data=f"quality_{q}"))
    else:
        for resolution, size in size_map.items():
            quality = resolution_to_quality.get(resolution)
            if quality and quality in predefined_quality_order:
                size_mb = round(size, 1)
                buttons.append(InlineKeyboardButton(
                    text=f"{quality} ({size_mb} MB)",
                    callback_data=f"quality_{quality}"
                ))

    # Добавим кнопку "Только аудио"
    buttons.append(InlineKeyboardButton(text="🔊 Только аудио", callback_data="quality_audio"))

    # Инлайн разметка в 2 столбца
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        buttons[i:i + 2] for i in range(0, len(buttons), 2)
    ])

    current_links[user.id] = text
    await message.answer("🎬 Выберите качество или только аудио:", reply_markup=keyboard)


async def handle_quality_selection_callback(call: CallbackQuery):
    user_id = call.from_user.id
    data = call.data

    if downloading_status.get(user_id):
        await call.answer("⏳ Уже идёт загрузка.", show_alert=True)
        return

    if user_id not in current_links:
        await call.message.edit_text("⚠️ Пожалуйста, сначала отправьте ссылку на видео.")
        return

    quality_raw = data.replace("quality:", "")
    url = current_links.pop(user_id)
    downloading_status[user_id] = True

    # ❌ Удаление предыдущего сообщения с кнопками
    try:
        await call.message.delete()
    except Exception as e:
        log_action(f"❗️Не удалось удалить сообщение с кнопками: {e}")

    if quality_raw == "audio":
        quality = "0"
        download_type = "audio"
    else:
        quality = quality_raw.split(" ")[0].replace("p", "")
        download_type = "video"

    # ⏳ Отправка прогресс-сообщения
    msg = await call.message.answer("🔄 Скачивание началось...")
    asyncio.create_task(download_and_send_wrapper(user_id, url, download_type, quality, msg))


async def download_and_send_wrapper(user_id, url, download_type, quality):

    try:
        await download_and_send(user_id, url, download_type, quality)
    except Exception as e:
        await bot.edit_message_text(
            f"❌ Ошибка при скачивании: {e}",
            chat_id=user_id,
            message_id=progress_messages.get(user_id, 0)
        )
    finally:
        downloading_status.pop(user_id, None)
        progress_messages.pop(user_id, None)
