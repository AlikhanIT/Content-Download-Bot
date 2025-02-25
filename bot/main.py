import asyncio
import requests
from bot.handlers.start_handler import start
from bot.handlers.video_handler import handle_link, handle_quality_selection
from bot.utils.log import log_action
from bot.utils.downloader import check_ffmpeg_installed
from config import bot, dp

async def main():
    try:
        # Получаем и выводим внешний IP
        ip = requests.get('https://api.ipify.org').text
        log_action("Внешний IP сервера", ip)

        check_ffmpeg_installed()
    except EnvironmentError as e:
        log_action("Ошибка запуска", str(e))
        exit(1)
    except Exception as e:
        log_action("Ошибка получения IP", str(e))

    # Регистрация хендлеров
    dp.message.register(start, lambda msg: msg.text == "/start")
    dp.message.register(handle_link, lambda msg: msg.text.startswith("http"))
    dp.message.register(handle_quality_selection, lambda msg: "p" in msg.text or msg.text == "Только аудио")

    log_action("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
