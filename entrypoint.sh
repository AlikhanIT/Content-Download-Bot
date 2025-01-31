#!/bin/sh
set -e

# Запускаем NordVPN демон
echo "Starting NordVPN daemon..."
/usr/bin/nordvpnd --daemon --pidfile /run/nordvpn/nordvpnd.pid

# Даем время демону для инициализации
echo "Waiting for daemon initialization..."
sleep 5

# Авторизация с токеном
echo "Logging in with token..."
nordvpn login --token "$NORDVPN_TOKEN"

# Подключение к VPN
echo "Connecting to VPN..."
nordvpn connect

# Проверка статуса
echo "VPN Status:"
nordvpn status

# Проверка IP
echo "Current IP:"
curl -s https://ifconfig.me
echo

# Запуск бота
echo "Starting bot..."
exec /app/venv/bin/python bot/main.py