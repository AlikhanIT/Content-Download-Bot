import asyncio
import subprocess
from bot.utils.common import get_external_ip
from bot.utils.log import log_action
from bot.utils.downloader import check_ffmpeg_installed
from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, handle_quality_selection
from config import bot, dp

async def reconnect_vpn():
    while True:
        try:
            # Получение и логирование IP
            ip = get_external_ip()
            log_action("Внешний IP сервера:", ip)

            # Проверка состояния NordVPN
            service_check = subprocess.run(
                ["nordvpn", "status"], capture_output=True, text=True, check=False
            )
            if "You are not connected to NordVPN" in service_check.stdout or \
                    "nordvpnd.sock not found" in service_check.stderr:
                log_action("NordVPN не запущен, пытаемся стартовать службу...")
                subprocess.run(["/etc/init.d/nordvpn", "start"], check=True)
                await asyncio.sleep(5)

            # Переподключение к NordVPN
            subprocess.run(["nordvpn", "disconnect"], check=True)
            subprocess.run(["nordvpn", "connect"], check=True)
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

    # Запуск фоновой задачи VPN
    asyncio.create_task(reconnect_vpn())

    # Регистрация хендлеров
    dp.message.register(start, lambda msg: msg.text == "/start")
    dp.message.register(handle_link, lambda msg: msg.text.startswith("http"))
    dp.message.register(handle_quality_selection, lambda msg: "p" in msg.text or msg.text == "Только аудио")

    log_action("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
