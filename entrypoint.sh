#!/bin/sh
set -e  # Останавливаем скрипт при ошибке

echo "Starting NordVPN daemon..."
/usr/sbin/nordvpnd --daemon --syslog

# Даем NordVPN время запуститься
sleep 5

echo "Checking if already logged in..."
if ! nordvpn account; then
  echo "Logging in with token..."
  nordvpn login --token "$NORDVPN_TOKEN" || { echo "Login failed"; exit 1; }
fi

echo "Connecting to VPN..."
nordvpn connect --country "United_States" || { echo "Connection failed"; exit 1; }

# Дополнительная пауза для стабилизации соединения
sleep 5

echo "VPN Status:"
nordvpn status

echo "Current IP:"
curl -s ifconfig.me || echo "Failed to retrieve IP"

# Проверка интернет-соединения
echo "Checking internet connection..."
ping -c 3 8.8.8.8 || { echo "No internet access via VPN!"; exit 1; }

# Запуск основного приложения
echo "Starting main application..."
exec /app/venv/bin/python bot/main.py
