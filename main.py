"""
Точка входа для хостингов, которые по умолчанию запускают main.py.
Сам бот — в bot.py (python bot.py работает так же).

На хостинге сборка идёт через собственный Dockerfile (см. его шапку).
"""
import asyncio

from bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
