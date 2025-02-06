#!/bin/sh
set -e

echo "Starting NordVPN daemon..."
/usr/sbin/nordvpnd --daemon --syslog

# Даем NordVPN время запуститься
sleep 5

echo "Logging in to NordVPN..."
nordvpn login --token "$NORDVPN_TOKEN" || { echo "Login failed"; exit 1; }

echo "Connecting to VPN..."
nordvpn connect --country "United_States" || { echo "Connection failed"; exit 1; }

# Дополнительная пауза, чтобы соединение стабилизировалось
sleep 5

echo "VPN Status:"
nordvpn status

echo "Current IP:"
curl -s ifconfig.me

echo "Starting main application..."
exec /app/venv/bin/python bot/main.py
