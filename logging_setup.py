"""
Единая настройка логирования для бота и веб-панели.

Раньше половина кода печатала диагностику через print() — это значит,
что при запуске под systemd/службой Windows сообщения терялись или шли
вперемешку без времени и уровня, и разобраться, почему, например, не
сгенерировалась картинка, было невозможно. Теперь всё идёт через logging
с ротацией файла, чтобы логи не съели диск за месяц работы.
"""
import logging
import logging.handlers
import sys

from paths import LOGS_DIR, ensure_dirs

LOGGER_NAME = "police-news-bot"

_configured = False


def setup_logging(level: str = "INFO", to_file: bool = True) -> logging.Logger:
    """Идемпотентно: повторные вызовы не плодят обработчики (иначе каждая
    строка лога дублировалась бы столько раз, сколько раз позвали setup)."""
    global _configured

    root = logging.getLogger()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric_level)

    if _configured:
        return logging.getLogger(LOGGER_NAME)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if to_file:
        try:
            ensure_dirs()
            file_handler = logging.handlers.RotatingFileHandler(
                LOGS_DIR / "bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Не удалось открыть файл лога: %s", exc)

    # Библиотеки болтливы на DEBUG — приглушаем, чтобы наши сообщения
    # не тонули в трассировке HTTP-запросов.
    for noisy in ("httpx", "httpcore", "urllib3", "aiogram.event", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return logging.getLogger(LOGGER_NAME)


def get_logger(suffix: str | None = None) -> logging.Logger:
    """get_logger("image") -> логгер "police-news-bot.image"."""
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
