import requests
from bot.utils.log import log_action


def get_external_ip():
    ip_services = [
        "https://checkip.amazonaws.com",
        "https://ifconfig.me",
        "https://ipinfo.io/ip",
        "https://icanhazip.com"
    ]

    for url in ip_services:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                ip = response.text.strip()
                log_action("Внешний IP сервера:", ip)
                return ip
        except requests.RequestException as e:
            log_action(f"Ошибка получения IP с {url}", str(e))

    log_action("Ошибка получения IP", "Все сервисы недоступны")
    return None
