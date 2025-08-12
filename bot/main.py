# main.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Union, Optional

from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Update
from aiogram.exceptions import TelegramNetworkError

from bot.handlers.start_handler import start
from bot.handlers.video_handler import (
    handle_link,
    download_and_send_wrapper,
    current_links,
    downloading_status,
)
from bot.utils.log import log_action
from bot.utils.tor_port_manager import initialize_all_ports_once
# import оставлен на будущее (не блокируйте старт лишними вызовами)
# from bot.utils.video_info import get_video_info_with_cache
from config import bot, dp, CHANNEL_IDS  # см. комментарии ниже


# -------------------- ЛОГИ --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


# -------------------------------------------------------------------
# РЕКОМЕНДАЦИИ ПО config.py
# -------------------------------------------------------------------
# 1) Сделайте так, чтобы CHANNEL_IDS был списком ИДЕНТИФИКАТОРОВ КАНАЛОВ
#    или @username. Примеры:
#    CHANNEL_IDS = [-1001234567890, "@my_public_channel"]
# 2) Для приватных каналов добавьте бота админом (с правом приглашать).
#    Если это невозможно — укажите публичные ссылки руками в CHANNEL_LINKS:
#    CHANNEL_LINKS = {
#        -1001234567890: "https://t.me/+STATIC_INVITE_CODE",
#        "@my_public_channel": "https://t.me/my_public_channel"
#    }
# 3) Если вы НЕ можете проверять подписку (бот не админ), выставьте:
#    REQUIRE_SUBSCRIPTION = False
#    Тогда бот будет работать без «жёсткой» проверки, но напоминать можно.
# -------------------------------------------------------------------

# Опциональные переменные, если хотите положить в config.py
try:
    from config import CHANNEL_LINKS  # dict[Union[int,str], str]
except Exception:
    CHANNEL_LINKS = {}

try:
    from config import REQUIRE_SUBSCRIPTION  # bool
except Exception:
    REQUIRE_SUBSCRIPTION = True


# -------------------- ERROR HANDLER --------------------
from aiogram import Router
router_sys = Router()


@router_sys.errors()
async def _errors_handler(update: Update, exception: Exception):
    logging.exception("Unhandled error: %r", exception)
    log_action("dp_error", repr(exception))
    # вернуть True, чтобы остановить дальнейшую обработку ошибки
    return True


dp.include_router(router_sys)


# -------------------- Подписки --------------------
# Кэш подписок
user_subscription_cache: dict[int, tuple[datetime, bool]] = {}  # user_id -> (last_check_dt, bool)


def _chat_display_title(chat: types.Chat) -> str:
    return chat.title or chat.username or str(chat.id)


async def _safe_get_invite_link(chat_id: Union[int, str]) -> Optional[str]:
    """
    Пробуем получить ссылку. Если бот не админ — вернём None.
    Для public @username формируем t.me/username.
    Для приватных — берём из CHANNEL_LINKS (если задано).
    """
    # Сначала явный оверрайд из конфигурации
    if chat_id in CHANNEL_LINKS:
        return CHANNEL_LINKS[chat_id]

    # Если это @username — можно отдать публичную ссылку
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        return f"https://t.me/{chat_id.lstrip('@')}"

    # Иначе пробуем получить ссылку из API (нужно право админа)
    try:
        chat = await bot.get_chat(chat_id)
        # если канал публичный (есть username)
        if chat.username:
            return f"https://t.me/{chat.username}"

        # приватный — пробуем экспорт (нужны права)
        invite_link = await chat.export_invite_link()
        return invite_link
    except Exception as e:
        # нет прав или приватный канал — возвращаем None, пусть ссылка берётся из CHANNEL_LINKS
        log_action("Invite link error", f"{chat_id}: {e}")
        return None


async def _is_member(chat_id: Union[int, str], user_id: int) -> bool:
    """
    Проверка подписки. Если нет прав или приватный канал — возвращаем False,
    но не валим бота.
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        # Если мы не можем проверить (бот не админ/канал приватный), поведение решаем флагом
        log_action("get_chat_member error", f"chat={chat_id}, user={user_id}, err={e}")
        return not REQUIRE_SUBSCRIPTION  # если проверка необязательна — пропускаем


async def check_subscription(user_id: int, force_check: bool = False) -> bool:
    """
    Проверяем подписку пользователя на все каналы из CHANNEL_IDS.
    Если REQUIRE_SUBSCRIPTION = False — возвращаем True даже при невозможности проверки.
    """
    try:
        if not force_check and user_id in user_subscription_cache:
            last_check, status = user_subscription_cache[user_id]
            if datetime.now() - last_check < timedelta(minutes=10):
                return status

        # пустой список каналов — считаем «подписан»
        if not CHANNEL_IDS:
            user_subscription_cache[user_id] = (datetime.now(), True)
            return True

        for channel_id in CHANNEL_IDS:
            if not await _is_member(channel_id, user_id):
                user_subscription_cache[user_id] = (datetime.now(), False)
                return False

        user_subscription_cache[user_id] = (datetime.now(), True)
        return True
    except Exception as e:
        log_action("Ошибка проверки подписки", f"User {user_id}: {e}")
        # Не валим бот — решаем по флагу
        return not REQUIRE_SUBSCRIPTION


async def send_subscription_reminder(user_id: int):
    """
    Показываем кнопки «Подписаться» (устойчиво, без обязательного export_invite_link).
    Каждая кнопка — своей строкой, чтобы Telegram не ругался на слишком длинный ряд.
    """
    try:
        rows: List[List[InlineKeyboardButton]] = []
        for channel_id in CHANNEL_IDS:
            # Подпись для кнопки
            try:
                chat = await bot.get_chat(channel_id)
                title = _chat_display_title(chat)
            except Exception:
                title = str(channel_id)

            # Ссылка
            link = await _safe_get_invite_link(channel_id)
            if not link:
                # если совсем нет — пропускаем кнопку, но логируем
                log_action("No link for channel", f"{channel_id}")
                continue

            rows.append([InlineKeyboardButton(text=f"Подписаться: {title}", url=link)])

        # Кнопка «Проверить подписки» — отдельной строкой
        rows.append([InlineKeyboardButton(text="✅ Проверить подписки", callback_data="check_subscription")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        await bot.send_message(
            user_id,
            "📢 Для использования бота необходимо подписаться на наши каналы:",
            reply_markup=keyboard
        )
    except Exception as e:
        log_action("Ошибка отправки напоминания", f"User {user_id}: {e}")


# -------------------- Хэндлеры --------------------
@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if await check_subscription(user_id, force_check=True):
        await callback.answer("✅ Вы подписаны на все каналы!", show_alert=True)
    else:
        await callback.answer("❌ Вы не подписаны на все каналы!", show_alert=True)
        await send_subscription_reminder(user_id)


@dp.message(Command("start"))
async def handle_start(message: types.Message):
    if not await check_subscription(message.from_user.id, force_check=True):
        await send_subscription_reminder(message.from_user.id)
        return
    await start(message)


@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("pong")


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


# -------------------- Утилиты старта --------------------
async def wait_bot_api_ready(bot_, attempts: int = 20):
    for i in range(attempts):
        try:
            me = await bot_.get_me()
            logging.info("Bot connected: @%s (id=%s)", me.username, me.id)
            return
        except TelegramNetworkError as e:
            logging.warning("Bot API not ready (%s), retry %d/%d", e, i + 1, attempts)
            await asyncio.sleep(1 + 0.5 * i)
    raise RuntimeError("Telegram Bot API server is not ready")


# -------------------- Запуск --------------------
async def main():
    # НИЧЕГО тяжёлого до старта поллинга!
    # Если нужны инициализации — запускаем как фоновые таски, чтобы не блокировать.
    try:
        proxy_ports = [9050]  # или из ENV/конфига
        test_url = None       # если хотите — укажите direct URL; None чтобы не блокировать
        if test_url:
            asyncio.create_task(initialize_all_ports_once(test_url, proxy_ports))
    except Exception as e:
        log_action("init tor ports error", str(e))

    # Фоновая задача (необязательно)
    asyncio.create_task(subscription_check_task())

    # Если был webhook — удаляем, иначе polling не будет получать апдейты
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook deleted (drop_pending_updates=True)")
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)

    # Ждём готовности Bot API (актуально и для локального, и для официального)
    await wait_bot_api_ready(bot)

    log_action("Бот запущен", "start_polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
