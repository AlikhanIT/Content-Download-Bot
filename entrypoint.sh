#!/bin/sh
set -e

# Создаём machine-id
dbus-uuidgen --ensure

# Проверяем и создаём каталоги NordVPN
mkdir -p /var/lib/nordvpn/data /run/nordvpn
touch /var/lib/nordvpn/data/version.dat

# Обновляем время
apt-get update && apt-get install -y tzdata
date -s "$(curl -s --head http://google.com | grep '^Date:' | cut -d' ' -f3-6)Z"

# Проверяем, установлен ли nordvpn
if ! command -v nordvpn >/dev/null 2>&1; then
  echo "NordVPN not found! Installing..."
  apt-get install -y nordvpn
fi

# Запускаем NordVPN
echo "Starting NordVPN..."
/usr/sbin/nordvpnd &

# Ждём запуск демона
timeout=30
while [ ! -S /run/nordvpn/nordvpnd.sock ] && [ $timeout -gt 0 ]; do
  sleep 1
  timeout=$((timeout - 1))
done

if [ ! -S /run/nordvpn/nordvpnd.sock ]; then
  echo "Failed to start NordVPN!"
  exit 1
fi

# Настройка VPN
nordvpn set technology nordlynx
nordvpn set killswitch on
nordvpn set autoconnect off

# Логинимся
echo "Logging in..."
echo "$NORDVPN_TOKEN" | nordvpn login --token

# Подключаемся
echo "Connecting to VPN..."
nordvpn connect || { echo "VPN connection failed!"; exit 1; }

# Запуск бота
exec /app/venv/bin/python bot/main.py
