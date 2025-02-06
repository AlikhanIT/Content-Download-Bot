#!/bin/sh
set -e

# Проверяем, установлен ли токен NordVPN
if [ -z "$NORDVPN_TOKEN" ]; then
  echo "Error: NORDVPN_TOKEN is not set. Exiting..."
  exit 1
fi

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
