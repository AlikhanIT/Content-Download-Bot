import os
import asyncio
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

# Загрузка переменных окружения
load_dotenv()

# Telegram Bot
API_TOKEN = os.getenv("API_TOKEN", "default_token")
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://localhost:8081")

# MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017")
DB_NAME = "proxy_manager"
COLLECTION_NAME = "proxies"

# Прокси настройки
CHECK_INTERVAL = 30  # Проверять прокси каждые 30 секунд
BAN_TIME = 900  # Время блокировки прокси (15 минут)
MAX_REQUESTS_PER_PROXY = 5  # Сколько запросов делать перед сменой IP
CHANNEL_IDS = [-1002396633073]  # Пример ID каналов

# Настраиваем сессию
session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL), timeout=1800)
bot = Bot(token=API_TOKEN, session=session)
dp = Dispatcher()

# Создаём глобальный семафор
semaphore = asyncio.Semaphore(10)
