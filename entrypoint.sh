#!/bin/sh
set -e

# Проверка наличия nordvpnd
if ! command -v nordvpnd >/dev/null 2>&1; then
  echo "NordVPN daemon not found! Reinstalling..."

  # Устанавливаем NordVPN через официальный APT-репозиторий
  apt-get update
  apt-get install -y nordvpn
fi

# Проверяем, что nordvpnd действительно существует
if [ ! -f /usr/sbin/nordvpnd ]; then
  echo "NordVPN daemon installation failed!"
  exit 1
fi

# Запускаем NordVPN демон
echo "Starting NordVPN daemon..."
/usr/sbin/nordvpnd &

# Ожидаем инициализации демона
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
if [ -z "$NORDVPN_TOKEN" ]; then
  echo "NORDVPN_TOKEN is not set!"
  exit 1
fi

echo "Logging in with token..."
echo "$NORDVPN_TOKEN" | nordvpn login --token

# Подключение к VPN
echo "Connecting to VPN..."
nordvpn connect || { echo "VPN connection failed!"; exit 1; }

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
