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

            # Переподключение к NordVPN
            subprocess.run(["nordvpn", "disconnect"], check=True)
            subprocess.run(["nordvpn", "connect"], check=True)
            log_action("VPN переподключен")
        except Exception as e:
            log_action("Ошибка при переподключении VPN", str(e))

        await asyncio.sleep(300)  # Повторяем каждые 5 минут


if __name__ == "__main__":
    asyncio.run(reconnect_vpn())
