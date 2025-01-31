#!/bin/sh
set -e

export PYTHONPATH=/app

# Проверяем, установлен ли NordVPN
if ! command -v nordvpn &> /dev/null; then
  echo "NordVPN не установлен! Устанавливаю..."
  apt-get update && apt-get install -y nordvpn
fi

# Подключение к VPN
echo "Connecting to VPN..."
nordvpn connect || echo "Failed to connect to VPN"

# Проверка статуса
echo "VPN Status:"
nordvpn status || echo "Could not retrieve VPN status"

# Проверка IP
echo "Current IP:"
curl -s https://ifconfig.me
echo

# Запуск бота
echo "Starting bot..."
exec /app/venv/bin/python bot/main.py
