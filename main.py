"""
Точка входа для хостингов, которые по умолчанию запускают main.py.
Сам бот — в bot.py (python bot.py работает так же).

Главный файл называется bot.py, а не telegram_bot.py: сборщик хостинга
ищет в коде строки «import telegram…» и, увидев «import telegram_bot»,
требует библиотеку python-telegram-bot, которой в проекте нет.
"""
import asyncio

from bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
