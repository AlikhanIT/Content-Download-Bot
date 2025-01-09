from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from bot.utils.video_info import get_video_info
from bot.utils.downloader import download_and_send
from bot.utils.log import log_action

current_links = {}
downloading_status = {}

async def handle_link(message: types.Message):
    user = message.from_user
    text = message.text.strip()

    log_action("Ссылка от пользователя", f"Пользователь: {user.id} ({user.username}), Ссылка: {text}")

    _, _, size_map = await get_video_info(text)

    keyboard_buttons = []
    for quality in ["144p", "360p", "720p", "1080p"]:
        if quality in size_map:
            size = size_map[quality]
            size_mb = size // (1024 * 1024)
            keyboard_buttons.append(KeyboardButton(text=f"{quality} ({size_mb}MB)"))

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

    if downloading_status.get(user.id):
        await message.answer("Видео уже скачивается. Пожалуйста, подождите.")
        return

    await message.answer("Начался процесс скачивания...")
    await download_and_send(user.id, url, download_type, quality)
