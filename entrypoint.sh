#!/bin/sh
set -e


# Запуск основного приложения
exec "/app/venv/bin/python" "bot/main.py"
