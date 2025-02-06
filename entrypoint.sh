#!/bin/sh
set -e

# Запускаем сервис NordVPN вручную (так как systemd отсутствует)
echo "Starting NordVPN service..."
/etc/init.d/nordvpn start

# Ждём, пока сервис запустится
sleep 5

# Вход с токеном
echo "Logging in to NordVPN..."
nordvpn login --token "$NORDVPN_TOKEN"

# Подключаемся к VPN
echo "Connecting to VPN..."
nordvpn connect --country "United_States"

# Установка переменной PYTHONPATH
export PYTHONPATH="/app"

# Запуск основного скрипта
exec /app/venv/bin/python -m bot.main
