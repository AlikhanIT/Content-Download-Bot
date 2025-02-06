#!/bin/sh
set -e

# Проверяем, установлен ли токен NordVPN
if [ -z "$NORDVPN_TOKEN" ]; then
  echo "Error: NORDVPN_TOKEN is not set. Exiting..."
  exit 1
fi

# Синхронизация времени, если контейнер отстает
echo "Syncing system time..."
ntpdate -u pool.ntp.org || echo "NTP sync failed, continuing..."

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

# Принудительное задание DNS
echo "Setting custom DNS..."
nordvpn set dns 1.1.1.1 8.8.8.8

# Отключаем Kill Switch и Firewall
nordvpn set killswitch off
nordvpn set firewall off

# Попытка подключения с разными технологиями
echo "Connecting to VPN..."
nordvpn set technology nordlynx || echo "Failed to set NordLynx, trying OpenVPN..."
nordvpn set protocol udp
nordvpn connect --country "United_States" || {
  echo "NordLynx failed, trying OpenVPN..."
  nordvpn set technology openvpn
  nordvpn set protocol tcp
  nordvpn connect --country "United_States" || { echo "All VPN connection attempts failed"; exit 1; }
}

# Ждем установления соединения
sleep 5

# Проверяем интернет-соединение
echo "Checking internet connection..."
VPN_IP=$(curl -s --max-time 10 ifconfig.me) || { echo "No internet after VPN connection"; exit 1; }
echo "VPN is active. Current IP: $VPN_IP"

# Установка переменной PYTHONPATH
export PYTHONPATH="/app"

# Запуск основного скрипта
exec /app/venv/bin/python -m bot.main
