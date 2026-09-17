# Сборка бота «Хороший полицейский».
#
# Собственный Dockerfile вместо автосборки хостинга: автосборщик ищет в
# коде подстроку «import telegram» и требует библиотеку
# python-telegram-bot, которой в проекте нет (бот работает на aiogram),
# из-за чего образ не собирался.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Шрифт с кириллицей для карточек-иллюстраций: в slim-образе шрифтов
# нет вовсе, и Pillow рисовал бы вместо русских букв пустые квадраты.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости отдельным слоем — при правке кода они не переустанавливаются.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p storage/images logs

CMD ["python", "main.py"]
