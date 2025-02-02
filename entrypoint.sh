#!/bin/sh
set -e

# Запуск демона NordVPN в фоновом режиме
echo "Starting NordVPN daemon..."
/usr/sbin/nordvpnd --daemon --syslog

# Даем демону время запуститься
sleep 5

# Логинимся с токеном
echo "Logging in with token..."
nordvpn login --token "$NORDVPN_TOKEN" || { echo "Login failed"; exit 1; }

# Подключаемся к VPN
echo "Connecting to VPN..."
nordvpn connect --country "United States" || { echo "Connection failed"; exit 1; }

# Дополнительная пауза для стабилизации соединения
sleep 5

# Проверка статуса VPN
echo "VPN Status:"
nordvpn status

# Получаем текущий IP после подключения к VPN
echo "Current IP:"
curl -s ifconfig.me

# Запуск приложения
exec /app/venv/bin/python bot/main.py
