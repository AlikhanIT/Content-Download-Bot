import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, Tuple, List

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram import Dispatcher, Bot

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

# -------------------- Конфиг из окружения -------------------- #
API_TOKEN = os.getenv("API_TOKEN", "").strip()
if not API_TOKEN:
    raise RuntimeError("API_TOKEN is not set")

LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://telegram_bot_api:8081").strip()

# Список каналов (ID через запятую или @username)
# Примеры: "-1001234567890,@mychannel"
_channel_env = os.getenv("CHANNEL_IDS", "").strip()
def parse_channels(raw: str) -> List[str]:
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]
CHANNEL_IDS: List[str] = parse_channels(_channel_env)

# -------------------- Логгер (минимальный) -------------------- #
def log_action(title: str, details: str = ""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {title}: {details}")

# -------------------- Глобальные хранилища -------------------- #
user_subscription_cache: Dict[int, Tuple[datetime, bool]] = {}  # user_id -> (last_check, status)
current_links: Dict[int, str] = {}  # user_id -> pending url
downloading_status: Dict[int, str] = {}  # user_id -> status

# -------------------- Подключение к локальному Bot API -------------------- #
# aiogram v3: используем кастомный endpoint
server = TelegramAPIServer.from_base(LOCAL_API_URL)
session = AiohttpSession(api=server)
bot = Bot(token=API_TOKEN, session=session, parse_mode="HTML")
dp = Dispatcher()


# -------------------- Проверка подписки -------------------- #
async def check_subscription(user_id: int, force_check: bool = False) -> bool:
    """
    True -> подписан или проверку нельзя корректно выполнить
    False -> точно не подписан
    """
    try:
        # Если каналы не заданы — не блокируем
        if not CHANNEL_IDS:
            return True

        if not force_check and user_id in user_subscription_cache:
            last_check, status = user_subscription_cache[user_id]
            if datetime.now() - last_check < timedelta(minutes=10):
                return status

        for channel_id in CHANNEL_IDS:
            try:
                chat_member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
                if chat_member.status not in ('member', 'administrator', 'creator'):
                    user_subscription_cache[user_id] = (datetime.now(), False)
                    return False
            except Exception as e:
                # Если бот не может проверить (не в канале/нет прав) — не блокируем пользователя
                log_action("Проверка подписки: пропускаем из-за ошибки", f"{channel_id=} {e}")
                user_subscription_cache[user_id] = (datetime.now(), True)
                return True

        user_subscription_cache[user_id] = (datetime.now(), True)
        return True

    except Exception as e:
        log_action("Ошибка проверки подписки (глобальная)", f"User {user_id}: {e}")
        # На всякий случай не блокируем
        return True


async def send_subscription_reminder(user_id: int):
    """
    Шлём кнопки на каналы. Если нет прав на export_invite_link — даём t.me ссылки (если есть username).
    """
    try:
        if not CHANNEL_IDS:
            return

        rows = []
        for channel_id in CHANNEL_IDS:
            try:
                chat = await bot.get_chat(channel_id)
                # пытаемся получить инвайт (нужны права админа)
                try:
                    invite_link = await chat.export_invite_link()
                    url = invite_link
                except Exception:
                    if chat.username:
                        url = f"https://t.me/{chat.username}"
                    else:
                        url = None

                text = f"Подписаться на {chat.title}"
                if url:
                    rows.append([InlineKeyboardButton(text=text, url=url)])
                else:
                    rows.append([InlineKeyboardButton(
                        text=f"📢 {chat.title} (добавьте бота в админы для инвайта)",
                        callback_data="noop"
                    )])

            except Exception as e:
                log_action("Не удалось получить инфо канала", f"{channel_id=} {e}")

        # Кнопка перепроверки
        rows.append([InlineKeyboardButton(text="✅ Проверить подписки", callback_data="check_subscription")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        await bot.send_message(
            user_id,
            "📢 Для использования бота подпишитесь на наши каналы:",
            reply_markup=keyboard
        )
    except Exception as e:
        log_action("Ошибка отправки напоминания", f"User {user_id}: {e}")


# -------------------- UI для выбора качества -------------------- #
def build_quality_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="360p", callback_data="quality_360p"),
            InlineKeyboardButton(text="480p", callback_data="quality_480p"),
        ],
        [
            InlineKeyboardButton(text="720p", callback_data="quality_720p"),
            InlineKeyboardButton(text="1080p", callback_data="quality_1080p"),
        ],
        [
            InlineKeyboardButton(text="🔊 Аудио", callback_data="quality_audio"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# -------------------- Хендлеры -------------------- #
@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if await check_subscription(user_id, force_check=True):
        await callback.answer("✅ Вы подписаны на все каналы!", show_alert=True)
    else:
        await callback.answer("❌ Вы не подписаны на все каналы!", show_alert=True)
        await send_subscription_reminder(user_id)


@dp.message(Command("ping"))
async def ping_cmd(message: types.Message):
    await message.answer("pong")


@dp.message(Command("start"))
async def handle_start(message: types.Message):
    log_action("HANDLE /start", f"from={message.from_user.id}")
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return

    await message.answer(
        "Привет! Отправь ссылку (YouTube, и т.п.), а я предложу варианты качества.\n"
        "Для проверки напиши /ping"
    )


@dp.message(F.text.startswith("http"))
async def handle_url(message: types.Message):
    log_action("HANDLE URL", f"from={message.from_user.id} text={message.text[:120]}")
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return

    url = message.text.strip()
    current_links[message.from_user.id] = url

    await message.answer(
        "Выбери качество для загрузки:",
        reply_markup=build_quality_keyboard()
    )


@dp.callback_query(lambda c: c.data.startswith("quality_"))
async def video_quality_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if not await check_subscription(user_id, force_check=True):
        await send_subscription_reminder(user_id)
        return

    data = callback_query.data.replace("quality_", "")
    if data == "audio":
        download_type = "audio"
        quality = "0"
    else:
        download_type = "video"
        quality = data.replace("p", "")

    url = current_links.pop(user_id, None)
    if not url:
        await callback_query.message.edit_text("❌ Истекло время выбора или ссылка потеряна. Отправьте заново.")
        return

    await callback_query.message.edit_text(f"⏳ Начинаю обработку: {download_type} {quality}p\n{url}")

    # ---- Симуляция загрузки/отправки (замени на свой пайплайн) ---- #
    try:
        downloading_status[user_id] = "running"
        await asyncio.sleep(2.0)  # здесь будет твой реальный загрузчик
        if downloading_status.get(user_id) == "cancelled":
            await callback_query.message.answer("🚫 Загрузка отменена пользователем.")
            downloading_status[user_id] = "idle"
            return

        # В реальном коде — отправка файла/аудио/документа…
        await callback_query.message.answer(
            f"✅ Готово (демо). Тип: {download_type}, качество: {quality}p\nURL: {url}"
        )
        downloading_status[user_id] = "idle"
    except Exception as e:
        downloading_status[user_id] = "idle"
        await callback_query.message.answer(f"❌ Ошибка при обработке: {e}")


@dp.callback_query(lambda c: c.data == "cancel")
async def cancel_download(call: CallbackQuery):
    user_id = call.from_user.id
    downloading_status[user_id] = "cancelled"
    await call.message.edit_text("🚫 Загрузка отменена.")


# -------------------- Бэкграунд-задачи (опционально) -------------------- #
async def subscription_check_task():
    while True:
        await asyncio.sleep(24 * 3600)
        log_action("Периодическая проверка подписок", "Запущено")


# -------------------- Точка входа -------------------- #
async def main():
    # Любая твоя инициализация — в фоне
    asyncio.create_task(subscription_check_task())
    log_action("Бот запущен", f"LOCAL_API_URL={LOCAL_API_URL}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
