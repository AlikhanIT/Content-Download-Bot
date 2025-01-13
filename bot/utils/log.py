import logging

# Настройка логгера
logger = logging.getLogger("BotLogger")
logger.setLevel(logging.INFO)  # Можно поменять на DEBUG для детальных логов

# Обработчик для вывода в консоль
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Формат логов
formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_handler.setFormatter(formatter)

# Добавляем обработчик к логгеру
logger.addHandler(console_handler)

# Функция логирования
def log_action(action, details=""):
    logger.info(f"{action}: {details}")
