#!/bin/sh
set -e

# Запускаем сервис NordVPN
echo "Starting NordVPN service..."
/etc/init.d/nordvpn start

# Ждём запуск
sleep 5

# Вход с токеном
echo "Logging in to NordVPN..."
nordvpn login --token "$NORDVPN_TOKEN"

# Подключаемся к случайному серверу
echo "Connecting to a random VPN server..."
nordvpn connect || { echo "Connection failed"; exit 1; }

# Установка переменной PYTHONPATH
export PYTHONPATH="/app/bot"

# Запуск основного скрипта
exec /app/venv/bin/python -m bot.main
