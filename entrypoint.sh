#!/bin/sh
set -e

# Запускаем NordVPN демон
echo "Starting NordVPN daemon..."
/usr/bin/nordvpnd --daemon --pidfile /run/nordvpn/nordvpnd.pid

# Ожидаем инициализации демона
echo "Waiting for daemon to start..."
while [ ! -S /run/nordvpn/nordvpnd.sock ]; do
    sleep 1
done

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