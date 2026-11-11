"""
Точка входа для хостингов, которые по умолчанию запускают main.py.
Сам бот — в bot.py (python bot.py работает так же).

Вместе с ботом поднимается веб-сервер (app.py): панель модерации и
витрина для экрана /screen. Хостинг запускает один процесс, поэтому
сервер живёт в отдельном потоке — иначе витрину на сервере просто
некому было бы отдать. Выключается переменной WEB_ENABLED=false.

На хостинге сборка идёт через собственный Dockerfile (см. его шапку).
"""
import asyncio
import threading

from bot import main
from config import settings
from logging_setup import get_logger

logger = get_logger("main")


def _serve_web() -> None:
    """Веб-сервер в фоне. Любая его поломка не должна ронять бота:
    публикация новостей важнее панели и витрины."""
    try:
        import uvicorn

        from app import app
    except ImportError as exc:
        logger.warning(
            "Веб-часть не запущена (%s). Установите fastapi и uvicorn, "
            "если нужны панель модерации и витрина /screen.", exc,
        )
        return

    logger.info(
        "Веб-сервер на http://%s:%d — витрина /screen",
        settings.web_host, settings.web_port,
    )
    try:
        uvicorn.run(
            app, host=settings.web_host, port=settings.web_port,
            log_level="warning",
        )
    except Exception:
        logger.exception("Веб-сервер остановлен, бот продолжает работу")


if __name__ == "__main__":
    if settings.web_enabled:
        threading.Thread(target=_serve_web, daemon=True).start()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
