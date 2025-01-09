from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def create_quality_keyboard(size_map):
    keyboard_buttons = []
    for quality in ["144p", "360p", "720p", "1080p"]:
        if quality in size_map:
            size = size_map[quality]
            size_mb = size // (1024 * 1024)
            keyboard_buttons.append(KeyboardButton(text=f"{quality} ({size_mb}MB)"))

    # Добавляем кнопку "Только аудио"
    keyboard_buttons.append(KeyboardButton(text="Только аудио"))

    keyboard = ReplyKeyboardMarkup(
        keyboard=[keyboard_buttons[i:i + 2] for i in range(0, len(keyboard_buttons), 2)],
        resize_keyboard=True,
        one_time_keyboard=True
    )


