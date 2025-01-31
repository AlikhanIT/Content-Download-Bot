#!/bin/sh
set -e

# Проверка, установлен ли NordVPN
if ! command -v nordvpn &> /dev/null; then
  echo "NordVPN не установлен! Устанавливаю..."
  apt-get update && apt-get install -y nordvpn
fi

# Настройка параметров VPN
echo "Configuring NordVPN..."
nordvpn set technology nordlynx || echo "Failed to set technology"
nordvpn set killswitch on || echo "Failed to enable killswitch"
nordvpn set autoconnect off || echo "Failed to disable autoconnect"

# Авторизация (если задан токен)
if [ -n "$NORDVPN_TOKEN" ]; then
  echo "Logging in with token..."
  nordvpn login --token "$NORDVPN_TOKEN"
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
