import requests

from bot.utils.log import log_action


def get_external_ip():
    try:
        ip = requests.get('https://api.ipify.org').text
        log_action("Внешний IP сервера:", ip)
    except Exception as e:
        log_action("Ошибка получения IP", str(e))
        ip = None
    return ip