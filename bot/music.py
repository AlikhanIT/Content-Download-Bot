import yt_dlp
from yt_dlp.utils import DownloadError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
import json
import os
import logging
from functools import lru_cache

from bot.config import API_TOKEN

# Настройки yt_dlp с конвертацией в MP3
YDL_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(title)s.%(ext)s',
    'quiet': True,
    'socket_timeout': 200,
    'noplaylist': True,
    'extract_flat': True,
    'cachedir': './cache',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }]
}

# Очередь для скачивания
download_queue = asyncio.Queue()

# Загрузка переводов
translations = {}
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

translations = {}

for lang in ['ru', 'en', 'kk']:
    file_path = os.path.join(os.getcwd(), "lang", f"{lang}.json")
    if os.path.exists(file_path):
        logger.info(f'File found: {file_path}')
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                translations[lang] = json.load(f)
        except json.JSONDecodeError:
            logger.error(f'Error decoding JSON in {file_path}')
            translations[lang] = {}
    else:
        logger.warning(f'File not found: {file_path}')
        translations[lang] = {}

# Функция получения перевода
def get_translation(context, key, **kwargs):
    language_code = context.user_data.get('language', 'ru')
    translation = translations[language_code].get(key, key)
    return translation.format(**kwargs)

# Глобальный словарь для хранения данных о пользователях
active_users = set()

# Загрузка данных о пользователях из файла
def load_users():
    global active_users
    try:
        with open('users.json', 'r', encoding='utf-8') as f:
            active_users = set(json.load(f))
    except FileNotFoundError:
        active_users = set()

# Сохранение данных о пользователях в файл
def save_users():
    with open('users.json', 'w', encoding='utf-8') as f:
        json.dump(list(active_users), f)

# Simple in-memory cache for search queries and downloads
search_cache = {}
download_cache = {}

# Кэширование результатов поиска
@lru_cache(maxsize=100)
def get_search_results(query):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        result = ydl.extract_info(f"ytsearch30:{query}", download=False)
        if 'entries' in result:
            return [video for video in result['entries'] if video.get('duration') is not None and video['duration'] <= 600]
        return []

# Кэширование скачанных файлов
def get_cached_file_path(video):
    return os.path.join('downloads', f"{video['id']}.mp3")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    # Добавляем пользователя в список активных
    if user_id not in active_users:
        active_users.add(user_id)
        save_users()

    language_buttons = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="set_language_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="set_language_en")],
        [InlineKeyboardButton("🇰🇿 Қазақша", callback_data="set_language_kk")]
    ]
    reply_markup = InlineKeyboardMarkup(language_buttons)
    await update.message.reply_text(get_translation(context, "choose_language"), reply_markup=reply_markup)

# Поиск музыки с кэшированием
async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем, что сообщение содержит текст
    if not update.message or not update.message.text:
        return  # Игнорируем сообщения без текста (например, фото)

    query = update.message.text.strip()

    # Проверяем, выполняется ли уже поиск для этого пользователя
    if context.user_data.get('is_searching', False):
        await update.message.reply_text(get_translation(context, "already_searching"))
        return

    if not query:
        await update.message.reply_text(get_translation(context, "empty_query"))
        return

    # Устанавливаем флаг, что поиск начат
    context.user_data['is_searching'] = True

    await update.message.reply_text(get_translation(context, "searching", query=query))

    # Проверяем кэшированный результат
    if query in search_cache:
        search_results = search_cache[query]
    else:
        search_results = get_search_results(query)
        search_cache[query] = search_results

    if not search_results:
        await update.message.reply_text(get_translation(context, "no_results"))
        # Очищаем состояние поиска
        context.user_data.pop('is_searching', None)
        return

    context.user_data['search_results'] = search_results
    context.user_data['page'] = 0
    await send_results_page(update, context)

# Отправка страницы с результатами поиска
async def send_results_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search_results = context.user_data.get('search_results', [])
    page = context.user_data.get('page', 0)

    start_idx = page * 5
    end_idx = start_idx + 5
    results_page = search_results[start_idx:end_idx]

    if not results_page:
        await update.callback_query.message.edit_text(get_translation(context, "no_more_results"))
        return

    buttons = []
    for idx, video in enumerate(results_page, start=start_idx + 1):
        buttons.append([InlineKeyboardButton(
            text=f"{idx}. {video['title']} ({video.get('duration', 'не указано')} сек.)",
            callback_data=f"select_{idx}"
        )])

    # Добавляем кнопки навигации, если они нужны
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(get_translation(context, "back"), callback_data="prev_page"))
    if end_idx < len(search_results):
        nav_buttons.append(InlineKeyboardButton(get_translation(context, "next"), callback_data="next_page"))

    # Добавляем кнопку "Отмена" в отдельную строку
    cancel_button = InlineKeyboardButton(get_translation(context, "cancel"), callback_data="cancel_search")
    buttons.append([cancel_button])

    # Если есть кнопки навигации, добавляем их в отдельную строку
    if nav_buttons:
        buttons.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(buttons)

    if update.message:
        await update.message.reply_text(get_translation(context, "choose_option"), reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(get_translation(context, "choose_option"),
                                                      reply_markup=reply_markup)

# Обработчик кнопок пагинации и выбора трека
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_search":
        # Очищаем все данные о поиске
        context.user_data.clear()
        await query.edit_message_text(get_translation(context, "search_cancelled"))
        return

    if query.data in ["prev_page", "next_page"]:
        if 'page' not in context.user_data:
            context.user_data['page'] = 0

        if query.data == "next_page":
            context.user_data['page'] += 1
        elif query.data == "prev_page":
            context.user_data['page'] -= 1

        await send_results_page(update, context)
        return

    if query.data.startswith("select_"):
        choice = int(query.data.split('_')[1]) - 1
        search_results = context.user_data.get('search_results', [])

        if 0 <= choice < len(search_results):
            video = search_results[choice]
            webpage_url = video.get('url') or video.get('webpage_url')

            if not webpage_url:
                await query.edit_message_text(get_translation(context, "download_error"))
                return

            current_message_text = query.message.text
            new_message_text = get_translation(context, "download", title=video['title'])

            if current_message_text != new_message_text:
                await query.edit_message_text(new_message_text)

            await download_queue.put((update, context, video, webpage_url))

            if download_queue.qsize() == 1:
                asyncio.create_task(process_download_queue())

# Функция обработки очереди загрузки
async def process_download_queue():
    while not download_queue.empty():
        update, context, video, webpage_url = await download_queue.get()

        # Проверка кэшированного файла
        cached_file_path = get_cached_file_path(video)
        if os.path.exists(cached_file_path):
            with open(cached_file_path, 'rb') as audio_file:
                await update.callback_query.message.reply_audio(audio=audio_file, title=video['title'])
            os.remove(cached_file_path)
        else:
            with ThreadPoolExecutor() as executor:
                loop = asyncio.get_event_loop()
                try:
                    info = await loop.run_in_executor(executor, lambda: yt_dlp.YoutubeDL(YDL_OPTS).extract_info(webpage_url, download=True))
                    file_path = yt_dlp.YoutubeDL(YDL_OPTS).prepare_filename(info).replace(info['ext'], 'mp3')

                    # Проверяем, доступен ли файл для работы
                    retry_attempts = 5
                    while retry_attempts > 0:
                        try:
                            with open(file_path, 'rb') as audio_file:
                                await update.callback_query.message.reply_audio(audio=audio_file, title=info.get('title'))
                            os.rename(file_path, cached_file_path)  # Сохраняем в кэш
                            break  # Выход из цикла, если файл успешно обработан
                        except PermissionError:
                            retry_attempts -= 1
                            time.sleep(1)  # Ждем секунду перед повторной попыткой

                    if retry_attempts == 0:
                        await update.callback_query.edit_message_text(get_translation(context, "download_error", error="File is in use"))

                    await update.callback_query.edit_message_text(get_translation(context, "downloaded"))

                except DownloadError as e:
                    await update.callback_query.edit_message_text(get_translation(context, "download_error", error=e))

        download_queue.task_done()
        # Очищаем состояние поиска после завершения загрузки
        context.user_data.pop('search_results', None)
        context.user_data.pop('page', None)
        context.user_data.pop('is_searching', None)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Placeholder logic for stats, for example, showing the number of active users.
    active_user_count = len(active_users)
    await update.message.reply_text(f"Active users: {active_user_count}")

# Function to set language
async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Get language code from callback data
    language_code = query.data.split('_')[-1]

    # Save language in user data
    context.user_data['language'] = language_code

    # Send a message about language change
    await query.edit_message_text(get_translation(context, "language_changed"))

    # Greet the user in the selected language
    await query.message.reply_text(get_translation(context, 'greeting'))

# Main function
def main():
    # Load user data
    load_users()

    application = Application.builder().token(API_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(set_language, pattern="^set_language_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_music))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Run the bot
    application.run_polling()

if __name__ == '__main__':
    main()
