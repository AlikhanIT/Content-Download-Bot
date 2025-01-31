#!/bin/sh
set -e

# Проверка наличия nordvpnd
if [ ! -f /usr/bin/nordvpnd ]; then
  echo "NordVPN daemon not found! Reinstalling..."
  curl -sSf https://downloads.nordcdn.com/apps/linux/install.sh | sh -s -- --nordvpn
fi

# Запускаем NordVPN демон
echo "Starting NordVPN daemon..."
/usr/bin/nordvpnd --daemon --pidfile /run/nordvpn/nordvpnd.pid

# Ожидаем инициализации демона (добавляем проверку сокета)
echo "Waiting for daemon to start..."
timeout=30
while [ ! -S /run/nordvpn/nordvpnd.sock ] && [ $timeout -gt 0 ]; do
  sleep 1
  timeout=$((timeout - 1))
done

if [ ! -S /run/nordvpn/nordvpnd.sock ]; then
  echo "Failed to start NordVPN daemon!"
  exit 1
fi

# Настройка параметров VPN
echo "Configuring NordVPN..."
nordvpn set technology nordlynx
nordvpn set killswitch on
nordvpn set autoconnect off

# Авторизация
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