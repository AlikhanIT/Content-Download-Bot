#!/bin/sh
set -e

export PYTHONPATH=/app

echo "Starting NordVPN service via systemctl..."
systemctl enable --now nordvpnd

# Дадим время на запуск демона
sleep 10

echo "Connecting to VPN..."
nordvpn connect || echo "Failed to connect to VPN"

echo "VPN Status:"
nordvpn status || echo "Could not retrieve VPN status"

echo "Current IP:"
curl -s https://ifconfig.me
echo

echo "Starting bot..."
exec /app/venv/bin/python bot/main.py
