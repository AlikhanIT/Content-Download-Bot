#!/bin/bash
set -e

# Проверка установки NordVPN
if [ ! -f /usr/sbin/nordvpnd ]; then
  echo "NordVPN installation verification..."
  apt-get update
  apt-get install -y --reinstall nordvpn
fi

# Запуск демона
echo "Starting NordVPN daemon..."
/usr/sbin/nordvpnd --daemon --pidfile /run/nordvpn/nordvpnd.pid

# Ожидание инициализации
echo "Waiting for daemon (30s timeout)..."
for i in {1..30}; do
  [ -S /run/nordvpn/nordvpnd.sock ] && break
  sleep 1
done

if [ ! -S /run/nordvpn/nordvpnd.sock ]; then
  echo "ERROR: Failed to start NordVPN daemon!"
  journalctl -u nordvpnd --no-pager
  exit 1
fi

# Настройка подключения
echo "Configuring VPN..."
nordvpn set technology nordlynx
nordvpn set killswitch on
nordvpn set autoconnect off

echo "Logging in with token..."
nordvpn login --token "$NORDVPN_TOKEN"

echo "Connecting to VPN..."
nordvpn connect

echo "Connection details:"
nordvpn status
curl -s https://ifconfig.me --connect-timeout 10
echo

exec /app/venv/bin/python bot/main.py