"""
Адаптер источника — публичные Telegram-каналы, без бот-токена и без
авторизации: используется публичная веб-версия t.me/s/<channel>, которую
Telegram отдаёт всем, включая ботов, именно для таких случаев (тот же
механизм, которым пользуются сервисы вроде RSSHub/TGStat).

Интерфейс идентичен RSSSource: fetch() -> list[RawNewsItem], поэтому
pipeline/orchestrator.py работает с обоими типами источников одинаково.

Ограничения:
  - Отдаёт только последние ~20 постов на странице (без пагинации —
    для регулярного /fetch этого достаточно).
  - Посты без текста (чистое видео/фото без подписи) пропускаются —
    рерайтить нечего.
  - Если Telegram изменит вёрстку, fetch() вернёт пустой список и
    напишет об этом в лог, а не уронит весь сбор.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from logging_setup import get_logger
from storage.models import RawNewsItem

logger = get_logger("source.telegram")

# Нейтральный браузерный User-Agent: на кастомный "PoliceNewsBot/1.0"
# Telegram периодически отдавал урезанную страницу без постов.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# background-image:url('...') — кавычки бывают одинарные, двойные
# или отсутствуют вовсе, в зависимости от версии вёрстки Telegram.
_BG_IMAGE_RE = re.compile(r"background-image\s*:\s*url\(['\"]?(.+?)['\"]?\)")

# Хвосты вида «Подписаться | Прислать новость» в конце поста.
_PROMO_TAIL_RE = re.compile(
    r"\n\s*(?:подписаться|подпишись|прислать новость|наш канал|мы в|"
    r"читайте также|источник)\b.*$",
    re.IGNORECASE | re.DOTALL,
)


class TelegramChannelSource:
    def __init__(self, channel_username: str, source_name: str | None = None):
        self.channel_username = channel_username.lstrip("@")
        self.source_name = source_name or f"Telegram: @{self.channel_username}"
        self.base_url = f"https://t.me/s/{self.channel_username}"

    def fetch(self, limit: int = 20) -> list[RawNewsItem]:
        try:
            response = requests.get(
                self.base_url,
                timeout=20,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "ru-RU,ru;q=0.9",
                },
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Канал @%s недоступен: %s", self.channel_username, exc)
            return []

        # Telegram отдаёт UTF-8, но иногда без явного charset в заголовке,
        # и requests угадывает ISO-8859-1 — кириллица превращается в кашу.
        response.encoding = response.apparent_encoding or "utf-8"

        soup = BeautifulSoup(response.text, "html.parser")
        message_blocks = soup.select("div.tgme_widget_message")

        if not message_blocks:
            logger.warning(
                "На странице @%s не найдено постов — возможно, канал закрыт "
                "или изменилась вёрстка Telegram.",
                self.channel_username,
            )
            return []

        # Свежие посты внизу страницы, поэтому берём хвост списка.
        message_blocks = message_blocks[-limit:]
        items: list[RawNewsItem] = []

        for block in message_blocks:
            try:
                text = self._extract_text(block)
                if not text:
                    continue  # медиа без подписи — рерайтить нечего

                link = self._extract_link(block)
                if not link:
                    # Без ссылки на пост дедупликация по URL не работает,
                    # а сам пост невозможно проверить руками — пропускаем.
                    continue

                items.append(RawNewsItem(
                    source_name=self.source_name,
                    source_url=link,
                    title=text.split("\n")[0][:120],
                    raw_text=text,
                    published_at=self._extract_date(block),
                    image_url=self._extract_image_url(block),
                ))
            except Exception:
                logger.exception("Не удалось разобрать пост канала @%s",
                                 self.channel_username)
                continue

        return items

    @staticmethod
    def _extract_text(block) -> str:
        text_div = block.select_one("div.tgme_widget_message_text")
        if not text_div:
            return ""

        text = text_div.get_text(separator="\n").strip()
        text = _PROMO_TAIL_RE.sub("", text)
        # Схлопываем пустые строки и неразрывные пробелы, которыми
        # пресс-службы разделяют абзацы.
        text = text.replace("\xa0", " ")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _extract_link(block) -> str:
        post_id = block.get("data-post", "")
        return f"https://t.me/{post_id}" if post_id else ""

    @staticmethod
    def _extract_date(block) -> datetime:
        time_tag = block.select_one("div.tgme_widget_message_date time")
        if time_tag and time_tag.get("datetime"):
            try:
                parsed = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
                # Гарантируем aware-время: naive-даты потом ломают
                # сравнение с datetime.now(timezone.utc).
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    @staticmethod
    def _extract_image_url(block) -> str | None:
        """
        Ищем картинку в нескольких местах: одиночное фото, элемент
        альбома, превью видео и превью внешней ссылки. Раньше
        учитывался только первый вариант, поэтому посты-альбомы (а это
        добрая половина постов пресс-служб) оставались без фото.
        """
        selectors = (
            "a.tgme_widget_message_photo_wrap",
            ".tgme_widget_message_grouped_wrap a.tgme_widget_message_photo_wrap",
            "i.tgme_widget_message_video_thumb",
            "i.link_preview_image",
            "i.link_preview_video_thumb",
            "i.tgme_widget_message_roundvideo_thumb",
        )

        for selector in selectors:
            element = block.select_one(selector)
            if not element:
                continue
            match = _BG_IMAGE_RE.search(element.get("style", ""))
            if match:
                url = match.group(1).strip()
                if url.startswith("http"):
                    return url

        return None
