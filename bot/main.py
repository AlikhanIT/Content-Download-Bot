import asyncio
import subprocess
from bot.utils.common import get_external_ip
from bot.utils.log import log_action
from bot.utils.downloader import check_ffmpeg_installed
from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, handle_quality_selection
from config import bot, dp, LOCAL_API_URL
import aiohttp

# Проверяет, установлен ли NordVPN
def is_nordvpn_installed():
    try:
        result = subprocess.run(["nordvpn", "--version"], capture_output=True, text=True, check=True)
        return "NordVPN" in result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

# Проверяет доступность API сервера
async def is_api_available(url=LOCAL_API_URL):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                return response.status == 404
    except aiohttp.ClientError:
        return False

# Фоновая задача переподключения VPN
async def reconnect_vpn():
    if not is_nordvpn_installed():
        log_action("NordVPN не установлен. Пропускаем VPN-переподключение.")
        return

    while True:
        try:
            ip = get_external_ip()
            log_action("Внешний IP сервера:", ip)

            # Проверка состояния NordVPN
            service_check = subprocess.run(
                ["nordvpn", "status"], capture_output=True, text=True, check=False
            )
            if "You are not connected to NordVPN" in service_check.stdout or \
                    "nordvpnd.sock not found" in service_check.stderr:
                log_action("NordVPN не запущен, пытаемся стартовать службу...")
                subprocess.run(["/etc/init.d/nordvpn", "start"], check=False)
                await asyncio.sleep(5)

            log_action("Disconnect")
            # Переподключение к NordVPN
            # subprocess.run(["nordvpn", "disconnect"], check=False)
            # subprocess.run(["nordvpn", "connect"], check=False)
            log_action("VPN переподключен")
        except subprocess.CalledProcessError as e:
            log_action("Ошибка при переподключении VPN", str(e))
        except Exception as e:
            log_action("Неизвестная ошибка VPN", str(e))

        await asyncio.sleep(1800)  # Повторяем каждые 30 минут

async def main():
    try:
        # Проверка окружения
        get_external_ip()
        check_ffmpeg_installed()
    except EnvironmentError as e:
        log_action("Ошибка запуска", str(e))
        exit(1)

    # Проверка доступности API сервера перед стартом бота
    if not await is_api_available():
        log_action("Ошибка: Telegram API сервер недоступен. Завершение работы.")
        return

    # Запуск фоновой задачи VPN, если NordVPN установлен
    asyncio.create_task(reconnect_vpn())

    # Регистрация хендлеров
    dp.message.register(start, lambda msg: msg.text == "/start")
    dp.message.register(handle_link, lambda msg: msg.text.startswith("http"))
    dp.message.register(handle_quality_selection, lambda msg: "p" in msg.text or msg.text == "Только аудио")

    log_action("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
