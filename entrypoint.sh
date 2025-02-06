#!/bin/sh
set -e

echo "Starting NordVPN setup..."

# Проверяем корректность времени внутри контейнера
echo "Checking system time..."
date
if [ $? -ne 0 ]; then
    echo "Error: Unable to fetch system time."
    exit 1
fi

# Проверяем интернет-соединение перед запуском VPN
echo "Checking internet connection..."
ping -c 3 8.8.8.8 > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "Error: No internet connection."
    exit 1
fi

# Запуск демона NordVPN
echo "Starting NordVPN daemon..."
/usr/sbin/nordvpnd --daemon --syslog
sleep 5

# Логинимся с токеном
echo "Logging in with token..."
if ! nordvpn login --token "$NORDVPN_TOKEN"; then
    echo "Error: Login failed."
    exit 1
fi

# Подключаемся к VPN
echo "Connecting to VPN..."
if ! nordvpn connect --country "United States"; then
    echo "Error: Connection failed."
    exit 1
fi

# Ожидаем стабилизации соединения
sleep 10

# Проверяем статус VPN
echo "VPN Status:"
nordvpn status

# Получаем текущий IP после подключения
echo "Current IP:"
curl -s ifconfig.me || echo "Error: Unable to fetch IP."

# Запуск основного приложения
echo "Starting application..."
exec /app/venv/bin/python bot/main.py
