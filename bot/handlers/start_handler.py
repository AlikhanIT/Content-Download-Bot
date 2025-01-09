from aiogram import types
from bot.utils.log import log_action

async def start(message: types.Message):
    user = message.from_user
    log_action("Команда /start", f"Пользователь: {user.id} ({user.username})")
    await message.answer("Привет! Отправь мне ссылку на видео, и я помогу его скачать.")
