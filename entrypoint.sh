#!/bin/sh
set -e

# Проверяем, установлен ли токен NordVPN
if [ -z "$NORDVPN_TOKEN" ]; then
  echo "Error: NORDVPN_TOKEN is not set. Exiting..."
  exit 1
fi

# Даем NordVPN разрешение на выполнение
chmod +x /usr/sbin/nordvpnd

# Запуск демона NordVPN
echo "Starting NordVPN daemon..."
/usr/sbin/nordvpnd --daemon --syslog

# Ждем, пока демон поднимется
sleep 5

# Вход с токеном
echo "Logging in with token..."
nordvpn login --token "$NORDVPN_TOKEN" || { echo "Login failed"; exit 1; }

# Отключаем Kill Switch (если нужно)
nordvpn set killswitch off

# Подключаемся к VPN
echo "Connecting to VPN..."
nordvpn connect --country "United_States" || { echo "Connection failed"; exit 1; }

# Ждем установления соединения
sleep 5

# Проверяем статус
echo "VPN Status:"
nordvpn status

# Проверяем IP
echo "Current IP:"
curl -s ifconfig.me

# Запуск основного приложения
exec "/app/venv/bin/python" "bot/main.py"
