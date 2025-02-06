#!/bin/sh
set -e


# Установка переменной PYTHONPATH
export PYTHONPATH="/app"

# Запуск основного скрипта
exec /app/venv/bin/python -m bot.main