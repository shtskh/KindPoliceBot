"""
Адаптер источника — RSS-лента (СМИ, агрегаторы, пресс-службы с RSS).

Интерфейс единый для всех источников: fetch() -> list[RawNewsItem].
Это позволяет добавлять новые источники (Telegram-каналы, сайты МВД
без RSS и т.д.), не трогая остальной пайплайн.
"""
from __future__ import annotations

import calendar
import html
import re
from datetime import datetime, timezone

import feedparser

from logging_setup import get_logger
from storage.models import RawNewsItem

logger = get_logger("source.rss")

_TAG_RE = re.compile(r"<[^>]+>")


class RSSSource:
    def __init__(self, feed_url: str, source_name: str | None = None):
        self.feed_url = feed_url
        self.source_name = source_name or self._pretty_name(feed_url)

    @staticmethod
    def _pretty_name(feed_url: str) -> str:
        """«https://ria.ru/export/...» -> «ria.ru» — в карточке модерации
        домен читается лучше, чем длинный URL ленты."""
        match = re.search(r"https?://([^/]+)", feed_url)
        return match.group(1) if match else feed_url

    def fetch(self, limit: int = 20) -> list[RawNewsItem]:
        parsed = feedparser.parse(self.feed_url)

        # feedparser не бросает исключений: о проблеме сообщает через
        # bozo/bozo_exception, и без этой проверки битая лента молча
        # превращалась в «источник ничего не отдал».
        if getattr(parsed, "bozo", 0) and not parsed.entries:
            logger.warning(
                "Лента %s не разобрана: %s",
                self.feed_url, getattr(parsed, "bozo_exception", "неизвестно"),
            )
            return []

        items: list[RawNewsItem] = []
        for entry in parsed.entries[:limit]:
            link = entry.get("link", "")
            if not link:
                continue

            items.append(RawNewsItem(
                source_name=self.source_name,
                source_url=link,
                title=self._clean(entry.get("title", "")),
                raw_text=self._extract_text(entry),
                published_at=self._parse_date(entry),
                image_url=self._extract_image(entry),
            ))

        return items

    @staticmethod
    def _clean(raw: str) -> str:
        """
        Снимает HTML-разметку и мнемоники.

        В summary лент РИА/ТАСС лежит HTML («<p>…</p>», «&nbsp;»). Раньше
        он уходил в LLM как есть — модель тратила токены на теги, а при
        сбое API этот же HTML попадал в fallback-текст поста.
        """
        text = _TAG_RE.sub(" ", raw or "")
        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _extract_text(self, entry) -> str:
        # Часть лент кладёт полный текст в "content", часть — в "summary".
        content = entry.get("content")
        if content:
            try:
                return self._clean(content[0].get("value", ""))
            except (AttributeError, IndexError, TypeError):
                pass
        return self._clean(entry.get("summary", ""))

    @staticmethod
    def _extract_image(entry) -> str | None:
        """media_content / media_thumbnail / enclosure покрывают
        практически все российские новостные ленты."""
        for key in ("media_content", "media_thumbnail"):
            media = entry.get(key)
            if media:
                try:
                    url = media[0].get("url")
                    if url:
                        return url
                except (AttributeError, IndexError, TypeError):
                    continue

        for link in entry.get("links", []) or []:
            try:
                if link.get("rel") == "enclosure" and str(
                    link.get("type", "")
                ).startswith("image/"):
                    return link.get("href")
            except AttributeError:
                continue

        return None

    @staticmethod
    def _parse_date(entry) -> datetime:
        for key in ("published_parsed", "updated_parsed"):
            parsed_time = getattr(entry, key, None)
            if parsed_time:
                try:
                    return datetime.fromtimestamp(
                        calendar.timegm(parsed_time), tz=timezone.utc
                    )
                except (ValueError, OverflowError, TypeError):
                    continue
        return datetime.now(timezone.utc)
