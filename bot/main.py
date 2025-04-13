import asyncio
from datetime import datetime, timedelta

import requests
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, download_and_send_wrapper, current_links, downloading_status
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
    direct_url = "https://rr2---sn-5goeenes.googlevideo.com/videoplayback?expire=1744535398&ei=Biv7Z-LCMOyCy_sPnpPzYA&ip=118.67.205.51&id=o-AN6yYlkcWNaR3cD8TifGi6U-Etge5YxGm_1f58hidHPH&itag=247&aitags=133%2C134%2C135%2C136%2C137%2C160%2C242%2C243%2C244%2C247%2C248%2C271%2C278%2C313%2C394%2C395%2C396%2C397%2C398%2C399%2C400%2C401&source=youtube&requiressl=yes&xpc=EgVo2aDSNQ%3D%3D&bui=AccgBcMRKWuZwkqEehplm7chr2cod53tIP8KCEhDDkNAUkC4P0Cvk3ckzmo3AW2j_cOGIybxB8jvntMM&vprv=1&svpuc=1&mime=video%2Fwebm&ns=1qOM2nEOKhFGkBXd0FH8SBgQ&rqh=1&gir=yes&clen=202398438&dur=2840.082&lmt=1744378512894121&keepalive=yes&lmw=1&c=TVHTML5&sefc=1&txp=4437534&n=7qSgHaMAM_rNxQ&sparams=expire%2Cei%2Cip%2Cid%2Caitags%2Csource%2Crequiressl%2Cxpc%2Cbui%2Cvprv%2Csvpuc%2Cmime%2Cns%2Crqh%2Cgir%2Cclen%2Cdur%2Clmt&sig=AJfQdSswRQIhALWU6RiIVIsXWho3D8nUP7CatXUrx-KMO3NvGJH1MhJKAiAUdG_m4UOSkr25nkBwWuxlhwYOpffTQob90znh2lSPAg%3D%3D&rm=sn-hx3voboxu-2oil7l,sn-5fo-c33ee76,sn-npo667z&rrc=79,79,104&fexp=24350590,24350737,24350827,24350961,24351173,24351206,24351230,24351524,24351528,24351545,24351593,24351606,24351637&req_id=8c8a02448ffa3ee&rms=nxu,au&redirect_counter=3&cms_redirect=yes&cmsv=e&ipbypass=yes&met=1744521557,&mh=w2&mip=37.99.17.235&mm=30&mn=sn-5goeenes&ms=nxu&mt=1744520479&mv=u&mvi=2&pl=17&lsparams=ipbypass,met,mh,mip,mm,mn,ms,mv,mvi,pl,rms&lsig=ACuhMU0wRAIgYjFHRutDxIDXoLdssjNM3vk44CeCviyyfhYu3skWPoACIDIXqxr5Zl05LsDmophy-4rWsQ1QyIw7WXFpRIWToexH"
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


