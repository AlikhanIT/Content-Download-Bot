import asyncio
from datetime import datetime, timedelta

from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, download_and_send_wrapper, current_links, downloading_status
from bot.utils.log import log_action
from bot.utils.tor_port_manager import normalize_all_ports_forever_for_url, unban_ports_forever
from bot.utils.video_info import get_video_info_with_cache
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

async def main():
    await asyncio.sleep(60)
    yt_url = "https://www.youtube.com/watch?v=-uzC0K3ku5g"
    info = await get_video_info_with_cache(yt_url)
    #direct_url = await resolve_final_url(await extract_url_from_info(info, ["136"]))
    #direct_url = "https://rr5---sn-ixh7yn7d.googlevideo.com/videoplayback?expire=1744554827&ei=63b7Z_Y975zi3g-vsu_RCQ&ip=117.55.241.163&id=o-AA8tnDcFc1sSkWFteUd991dmjNCRe-bZhDmd5cpLeEZF&itag=18&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&bui=AccgBcOxXVfF9rVasGk43wLmn1cmo5yx81kuFdozm1Lq1WLGhRGlUQDPVdCwZAsSS8U1y2Rg__McE2xS&vprv=1&svpuc=1&mime=video%2Fmp4&ns=5brch8CGyhbNkEG0TTBkqXUQ&rqh=1&gir=yes&clen=188798506&ratebypass=yes&dur=2840.148&lmt=1744362057838279&lmw=1&c=TVHTML5&sefc=1&txp=4438534&n=VTGaFS6A2zTxRw&sparams=expire%2Cei%2Cip%2Cid%2Citag%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cratebypass%2Cdur%2Clmt&sig=AJfQdSswRAIgFHRmcVlYwlGvXsieN5JZuxuUcOeq4qtz6WCM5rMmcY8CIBd2Tdtjd_W9ZnNuv4tMNUMhTDcEGWBIvPOA9Ih4_y3D&title=%D0%91%D1%80%D0%B8%D1%82%D0%B0%D0%BD%D0%B8%D1%8F.%20%D0%9A%D0%B0%D0%BA%20%D0%B2%D0%B5%D0%BB%D0%B8%D0%BA%D0%B0%D1%8F%20%D0%B8%D0%BC%D0%BF%D0%B5%D1%80%D0%B8%D1%8F%20%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%B8%D1%82%D1%81%D1%8F%20%D0%B8%D0%B7%D0%B3%D0%BE%D0%B5%D0%BC%3F&rm=sn-jwvoapox-qxae7s,sn-qxaey7z&rrc=79,104&fexp=24350590,24350737,24350827,24350961,24351173,24351230,24351495,24351524,24351528,24351545,24351557,24351606,24351637,24351658,24351660&req_id=16d9d58c94dba3ee&cmsv=e&rms=rdu,au&redirect_counter=2&cms_redirect=yes&ipbypass=yes&met=1744550277,&mh=w2&mip=37.99.17.235&mm=29&mn=sn-ixh7yn7d&ms=rdu&mt=1744549977&mv=m&mvi=5&pl=24&lsparams=ipbypass,met,mh,mip,mm,mn,ms,mv,mvi,pl,rms&lsig=ACuhMU0wRAIgFpNVNdozCsLigWrbb1Cq5xpSD2pTPVbG7CmvO_PRcpgCIBWJopnBQi12JDGQK_DBCoBFbA-c9gmKmOwwva4IeosX"
    #log_action(direct_url)

    proxy_ports = [9050]

    log_action("Начало проверки пулов:")
    await asyncio.sleep(60)

    asyncio.create_task(initialize_all_ports_once(test_url, proxy_ports))

    asyncio.create_task(subscription_check_task())  # Только 1 раз!

    log_action("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


