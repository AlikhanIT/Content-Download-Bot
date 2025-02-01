import asyncio
import subprocess
from bot.utils.common import get_external_ip
from bot.utils.log import log_action


async def reconnect_vpn():
    while True:
        try:
            # Получение и логирование IP
            ip = get_external_ip()
            log_action("Внешний IP сервера:", ip)

            # Проверка наличия службы NordVPN
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

        await asyncio.sleep(300)  # Повторяем каждые 5 минут


if __name__ == "__main__":
    asyncio.run(reconnect_vpn())
