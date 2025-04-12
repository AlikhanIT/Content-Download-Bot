import asyncio
from datetime import datetime, timedelta

import requests
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, download_and_send_wrapper, current_links
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

    await callback_query.message.edit_text("🔄 Скачивание началось, ожидайте...")

    asyncio.create_task(download_and_send_wrapper(
        user_id=callback_query.from_user.id,
        url=url,
        download_type=download_type,
        quality=quality
    ))


async def main():
    await asyncio.sleep(60)
    yt_url = "https://www.youtube.com/watch?v=-uzC0K3ku5g"
    info = await get_video_info_with_cache(yt_url)
    #direct_url = await resolve_final_url(await extract_url_from_info(info, ["136"]))
    direct_url = "https://rr3---sn-5go7ynl6.googlevideo.com/videoplayback?expire=1744482572&ei=rFz6Z_LlH5aG0u8P7OnF8AQ&ip=78.40.109.6&id=o-AHVDelonGR4hNhcjhhI9HdF6ykuB9BiYhkPk4qbzFsu6&itag=136&aitags=133%2C134%2C135%2C136%2C137%2C160%2C242%2C243%2C244%2C247%2C248%2C278%2C394%2C395%2C396%2C397%2C398%2C399&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&bui=AccgBcMD0mmwbPP4gCGS0WcL8Vv2obqBHJ5O4nJ1IlXZUWZqoeQMmM6OvRw1OpOh3Bpg2OI__mE_K-no&vprv=1&svpuc=1&mime=video%2Fmp4&ns=U2A6xCdYBed1AKUyt5eXOGkQ&rqh=1&gir=yes&clen=32308481&dur=661.880&lmt=1743853963807915&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=5535534&n=WFChhgev8nQD3Q&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRgIhAKTInLyiuAPGumF2WR0heMpCPdXMkRq0MH49znGzkPu-AiEAyu2F1kSA4YSS0vwu5yZi3023vloQMJc4UYvnXY85e5M%3D&rm=sn-ug5on-5a5s7e,sn-n8vkl7r&rrc=79,104&fexp=24350590,24350737,24350827,24350961,24351173,24351229,24351430,24351524,24351528,24351545,24351606,24351637&req_id=fb17a2058dbda3ee&rms=rdu,au&redirect_counter=2&cms_redirect=yes&cmsv=e&ipbypass=yes&met=1744460987,&mh=XE&mip=37.99.17.190&mm=29&mn=sn-5go7ynl6&ms=rdu&mt=1744460736&mv=u&mvi=3&pl=17&lsparams=ipbypass,met,mh,mip,mm,mn,ms,mv,mvi,pl,rms&lsig=ACuhMU0wRgIhAOjOrC92sfVYAAggzDLPKz4TKvLkQ4vje-5HZuoXBaLqAiEA0Hl3p_x7gnZLdMK1RJrR8BdX45i8f61VTCxeUqQw-VA%3D"
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


