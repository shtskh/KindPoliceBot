"""
Проверка доступности источников с той машины, где запущен бот.

ЗАЧЕМ
-----
Сбор может приносить почти ноль новостей по причине, которую со стороны
разработчика не видно: сайт отвечает на запрос из России и молчит на
запрос с зарубежного хостинга. Так бывает у ведомственных сайтов.

Локально всё при этом работает идеально, и отладка превращается в спор
«у меня всё хорошо» — «а у меня нет». Эта проверка выполняется там же,
где живёт бот, и показывает по каждому источнику: ответил, ответил
пусто или не ответил вовсе.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import feedparser
import requests

from config import settings
from content.regions import region_for_code
from logging_setup import get_logger

logger = get_logger("source_check")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Сколько региональных лент проверять — полная проверка всех 81 заняла бы
# слишком долго для ответа в чат.
REGION_SAMPLE = 5
TELEGRAM_SAMPLE = 3


@dataclass
class SourceStatus:
    kind: str
    name: str
    items: int = 0
    error: str = ""
    seconds: float = 0.0

    @property
    def icon(self) -> str:
        if self.error:
            return "❌"
        return "✅" if self.items else "🫙"


def _check_rss(name: str, url: str) -> SourceStatus:
    started = time.monotonic()
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
        elapsed = time.monotonic() - started
        if response.status_code != 200:
            return SourceStatus("rss", name, 0, f"HTTP {response.status_code}", elapsed)
        parsed = feedparser.parse(response.content)
        return SourceStatus("rss", name, len(parsed.entries), "", elapsed)
    except Exception as exc:
        return SourceStatus(
            "rss", name, 0, f"{type(exc).__name__}", time.monotonic() - started
        )


def _check_telegram(channel: str) -> SourceStatus:
    started = time.monotonic()
    try:
        response = requests.get(
            f"https://t.me/s/{channel}",
            headers={"User-Agent": USER_AGENT}, timeout=25,
        )
        elapsed = time.monotonic() - started
        if response.status_code != 200:
            return SourceStatus(
                "telegram", f"@{channel}", 0, f"HTTP {response.status_code}", elapsed
            )
        posts = response.text.count('class="tgme_widget_message ')
        return SourceStatus("telegram", f"@{channel}", posts, "", elapsed)
    except Exception as exc:
        return SourceStatus(
            "telegram", f"@{channel}", 0, f"{type(exc).__name__}",
            time.monotonic() - started,
        )


def check_sources() -> list[SourceStatus]:
    """Проверяет по образцу каждого типа источников. Безопасно для вызова
    из чата: ограничена выборкой и таймаутами."""
    jobs: list = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs.append(pool.submit(_check_rss, "МВД России", "https://мвд.рф/news/rss"))

        if settings.mvd_rss_enabled:
            for code in settings.mvd_rss_regions[:REGION_SAMPLE]:
                region = region_for_code(code) or code
                jobs.append(pool.submit(
                    _check_rss, f"МВД: {region}", f"https://{code}.мвд.рф/news/rss"
                ))

        for url in settings.rss_sources[:6]:
            name = url.split("//")[-1].split("/")[0]
            jobs.append(pool.submit(_check_rss, name, url))

        for channel in settings.telegram_channels[:TELEGRAM_SAMPLE]:
            jobs.append(pool.submit(_check_telegram, channel))

        return [job.result() for job in jobs]


def format_report(statuses: list[SourceStatus]) -> str:
    """Готовый текст для отправки в чат."""
    lines = ["🔌 <b>Доступность источников с этого сервера</b>\n"]

    for kind, title in (("rss", "Ленты"), ("telegram", "Telegram")):
        group = [s for s in statuses if s.kind == kind]
        if not group:
            continue
        lines.append(f"<b>{title}</b>")
        for status in group:
            detail = status.error or f"{status.items} записей"
            lines.append(f"{status.icon} {status.name} — {detail} "
                         f"({status.seconds:.1f} с)")
        lines.append("")

    broken = [s for s in statuses if s.error or not s.items]
    if broken:
        mvd_broken = [s for s in broken if s.name.startswith("МВД")]
        lines.append(f"⚠️ Недоступно источников: {len(broken)} из {len(statuses)}")
        if mvd_broken and len(mvd_broken) >= REGION_SAMPLE:
            # Самый частый случай: сайты МВД не отвечают зарубежному
            # хостингу, хотя из России открываются нормально.
            lines.append(
                "\nПохоже, сайты МВД недоступны с этого сервера. Обычно это "
                "ограничение по стране: тогда нужен хостинг в России или "
                "прокси. Остальные источники при этом работают."
            )
    else:
        lines.append("✅ Все проверенные источники отвечают.")

    return "\n".join(lines).strip()
