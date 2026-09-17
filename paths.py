"""
Единая точка вычисления путей проекта.

Зачем отдельный модуль: раньше пути вроде "storage/images" и "storage/bot.db"
были относительными, то есть зависели от текущей рабочей директории. Если бота
запускали не из корня проекта (systemd, планировщик Windows, IDE с другим
working dir), картинки сохранялись в одно место, а бот искал их в другом —
os.path.exists(item.image_path) возвращал False и пост уходил без фото.

Теперь все пути считаются от расположения этого файла, поэтому работают
одинаково независимо от того, откуда запущен процесс.
"""
from __future__ import annotations

from pathlib import Path

# Корень проекта — папка, в которой лежит этот файл.
PROJECT_ROOT = Path(__file__).resolve().parent

STORAGE_DIR = PROJECT_ROOT / "storage"
IMAGES_DIR = STORAGE_DIR / "images"
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
LOGS_DIR = PROJECT_ROOT / "logs"

DEFAULT_DB_PATH = STORAGE_DIR / "bot.db"


def ensure_dirs() -> None:
    """Создаёт рабочие директории. Идемпотентно, зовётся при старте."""
    for directory in (STORAGE_DIR, IMAGES_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def resolve(path_like: str | Path) -> Path:
    """
    Приводит путь к абсолютному: относительные пути считаются от корня
    проекта, абсолютные остаются как есть.

    Нужно для обратной совместимости — в БД уже лежат записи с путями
    вида "storage/images/123.jpg", записанные старой версией кода.
    """
    path = Path(path_like)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def to_relative(path_like: str | Path) -> str:
    """
    Обратное преобразование — храним в БД путь относительно корня проекта.
    Так базу можно перенести на другой сервер вместе с папкой storage,
    и пути не сломаются (в отличие от абсолютных C:\\Users\\...).
    """
    path = Path(path_like).resolve()
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()
