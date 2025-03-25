import requests
import time
import random
import re
from bot.utils.log import log_action

# Файл со списком прокси
PROXY_FILE = "proxies.txt"

# Настройки ротации
CHECK_INTERVAL = 30  # Проверять прокси каждые 30 секунд
BAN_TIME = 36000  # Время бана (15 минут)
MAX_REQUESTS_PER_PROXY = 5  # Количество запросов перед сменой прокси

# Хранение данных о прокси
proxies = []
banned_proxies = {}  # { "ip:port": {"banned_at": timestamp, "unban_at": timestamp} }
proxy_usage_count = {}


def ban_proxy(proxy_url):
    """ Блокирует прокси на определенное время """
    # Используем регулярное выражение для извлечения IP и порта
    match = re.match(r'http://(?:.*):(?:.*)@([^:]+):(\d+)', proxy_url)
    if match:
        ip = match.group(1)
        port = match.group(2)
        proxy_key = f"{ip}:{port}"

        banned_proxies[proxy_key] = {
            "banned_at": time.time(),
            "unban_at": time.time() + BAN_TIME
        }

        log_action(f"🚫 Прокси {proxy_key} забанен на {BAN_TIME // 60} минут.")
    else:
        log_action(f"🚫 Не удалось извлечь IP и порт из прокси URL: {proxy_url}")

def load_proxies():
    """ Загружает прокси из файла """
    global proxies
    proxies = []
    try:
        with open(PROXY_FILE, "r") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) == 4:
                    ip, port, user, password = parts
                    proxies.append({
                        "ip": ip,
                        "port": port,
                        "user": user,
                        "password": password
                    })
    except FileNotFoundError:
        print(f"Файл {PROXY_FILE} не найден.")

    print(f"Загружено {len(proxies)} прокси.")
    return proxies


def check_proxy(proxy):
    """ Проверяет, работает ли прокси """
    ip, port, user, password = proxy["ip"], proxy["port"], proxy["user"], proxy["password"]
    proxy_url = f"http://{user}:{password}@{ip}:{port}"

    try:
        response = requests.get("http://www.google.com", proxies={"http": proxy_url, "https": proxy_url}, timeout=5)
        return response.status_code == 200
    except requests.RequestException:
        return False


def refresh_proxies():
    """ Обновляет список рабочих прокси, удаляя неактивные """
    global proxies
    print("Проверка доступности прокси...")
    new_proxies = []
    for proxy in proxies:
        proxy_key = f"{proxy['ip']}:{proxy['port']}"

        # Проверяем, разбанился ли прокси
        if proxy_key in banned_proxies:
            if time.time() >= banned_proxies[proxy_key]["unban_at"]:
                print(f"Прокси {proxy_key} разбанен.")
                del banned_proxies[proxy_key]  # Убираем из списка бана
                new_proxies.append(proxy)
        else:
            if check_proxy(proxy):
                new_proxies.append(proxy)

    proxies = new_proxies
    print(f"Доступно {len(proxies)} рабочих прокси.")



def get_available_proxy():
    """ Возвращает доступный прокси (с учетом бана и минимального количества запросов) """
    global proxies, banned_proxies, proxy_usage_count

    if not proxies:
        return None  # Если нет доступных прокси

    # Удаляем временно забаненные прокси
    current_time = time.time()
    available_proxies = [
        proxy for proxy in proxies if f"{proxy['ip']}:{proxy['port']}" not in banned_proxies or
                                      current_time >= banned_proxies[f"{proxy['ip']}:{proxy['port']}"]["unban_at"]
    ]

    if not available_proxies:
        return None

    # Выбираем прокси с наименьшим количеством запросов
    proxy = min(available_proxies, key=lambda p: proxy_usage_count.get(f"{p['ip']}:{p['port']}", 0))
    proxy_key = f"{proxy['ip']}:{proxy['port']}"

    # Увеличиваем счетчик использования
    proxy_usage_count[proxy_key] = proxy_usage_count.get(proxy_key, 0) + 1

    # Если прокси достиг лимита запросов, временно его баним
    if proxy_usage_count[proxy_key] >= MAX_REQUESTS_PER_PROXY:
        banned_proxies[proxy_key] = {
            "banned_at": time.time(),
            "unban_at": time.time() + BAN_TIME
        }
        proxy_usage_count[proxy_key] = 0  # Сбросить счетчик

    return proxy


def get_proxy_status():
    """ Возвращает статус всех прокси, включая забаненные с временем разбанивания """
    status = []
    for proxy in proxies:
        proxy_key = f"{proxy['ip']}:{proxy['port']}"
        if proxy_key in banned_proxies:
            status.append({
                "ip": proxy["ip"],
                "port": proxy["port"],
                "status": "BANNED",
                "banned_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(banned_proxies[proxy_key]["banned_at"])),
                "unban_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(banned_proxies[proxy_key]["unban_at"]))
            })
        else:
            status.append({
                "ip": proxy["ip"],
                "port": proxy["port"],
                "status": "ACTIVE"
            })
    return status


