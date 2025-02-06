#!/bin/sh
set -e

# Запускаем сервис NordVPN вручную
echo "Starting NordVPN service..."
/etc/init.d/nordvpn start

# Ждём запуск
sleep 5

# Вход с токеном
echo "Logging in to NordVPN..."
nordvpn login --token "$NORDVPN_TOKEN"

# Подключаемся к VPN (правильный синтаксис)
echo "Connecting to VPN..."
nordvpn connect "United States" || { echo "Connection failed"; exit 1; }

# Установка переменной PYTHONPATH
export PYTHONPATH="/app"

# Запуск основного скрипта
exec /app/venv/bin/python -m bot.main
