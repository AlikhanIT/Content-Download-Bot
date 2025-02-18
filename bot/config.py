import os
import asyncio
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

# Загрузка переменных окружения из .env файла
load_dotenv()

# Получаем переменные из окружения
API_TOKEN = os.getenv("API_TOKEN", "default_token")
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://localhost:8081")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017")

# Настраиваем сессию
session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL), timeout=1800)
bot = Bot(token=API_TOKEN, session=session, timeout=1800)
dp = Dispatcher()

# Создаём глобальный семафор
semaphore = asyncio.Semaphore(10)
