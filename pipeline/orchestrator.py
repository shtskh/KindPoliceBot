"""
Общая точка входа в пайплайн обработки — используется и Telegram-ботом,
и (при желании) отдельным cron-скриптом. Веб-панель модерации этот файл
не вызывает — она только читает/обновляет уже собранные записи в БД.

Что здесь исправлено по сравнению с первой версией:

  * Источники опрашиваются параллельно. Раньше десять Telegram-каналов
    и два RSS запрашивались строго по очереди: один медленный источник
    задерживал весь сбор на свой таймаут.
  * Убран безусловный time.sleep(1) на КАЖДУЮ сырую новость, включая
    те, что отсеиваются дедупликацией без единого сетевого запроса.
    При 200 собранных постах это была лишняя пауза в 3 минуты на пустом
    месте. Пауза осталась только между реальными вызовами LLM.
  * Дешёвые проверки (дубликат, возраст, длина) идут ДО обращения к
    модели, а не после — это прямая экономия денег на API.
  * Подпись канала добавляется ПОСЛЕ генерации изображения. Раньше
    порядок был обратный, и в промпт генерации, и на карточку попадал
    футер со ссылками вместо сути новости.
  * Прогресс отдаётся через callback, чтобы бот мог показывать модератору
    «обработано 12 из 40», а не молчать несколько минут.
"""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from config import settings
from content.regions import region_for_code
from logging_setup import get_logger
from pipeline.image_pipeline import download_video, get_image_for_item
from pipeline.media_extract import extract_from_page
from pipeline.post_formatting import append_footer_if_missing
from pipeline.ranking import rank_candidates
from pipeline.rewrite import rewrite_news_item
from pipeline.verify import verify_news_item
from sources.keyword_filter import filter_by_keywords
from sources.rss_source import RSSSource
from sources.tg_channel_source import (
    TelegramChannelSource,
)
from storage import db
from storage.models import NewsItem, NewsStatus, RawNewsItem

logger = get_logger("pipeline")

# Пауза между обращениями к LLM — бережём лимиты провайдера.
PAUSE_BETWEEN_LLM_CALLS = 0.7

# Сколько источников опрашиваем одновременно. Источников стало около
# сотни (81 лента МВД + СМИ + Telegram), и по шесть потоков обход
# занимал бы минуты.
SOURCE_FETCH_WORKERS = 16

ProgressCallback = Callable[[str], None]


@dataclass
class CollectionStats:
    """Итоги цикла — бот показывает их модератору вместо голого числа."""
    raw_collected: int = 0
    duplicates: int = 0
    too_old: int = 0
    rejected: int = 0
    accepted: int = 0
    source_errors: list[str] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)
    empty_sources: list[str] = field(default_factory=list)
    mvd_collected: int = 0
    media_collected: int = 0
    telegram_collected: int = 0
    llm_checks: int = 0
    reject_breakdown: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def duration_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def summary_lines(self) -> list[str]:
        lines = [
            f"📥 Собрано из источников: {self.raw_collected}",
            f"      МВД: {self.mvd_collected} · "
            f"Telegram: {self.telegram_collected} · СМИ: {self.media_collected}",
            f"🔎 Проверено моделью: {self.llm_checks}",
            f"♻️ Пропущено как дубликаты: {self.duplicates}",
        ]
        if self.too_old:
            lines.append(f"🕰 Пропущено как устаревшие: {self.too_old}")
        lines.append(f"🚫 Отклонено фильтром: {self.rejected}")
        lines.append(f"✅ Отправлено на модерацию: {self.accepted}")
        lines.append(f"⏱ Заняло: {self.duration_seconds:.0f} сек.")
        if self.source_errors:
            lines.append("")
            lines.append(f"⚠️ Источники с ошибками: {len(self.source_errors)}")
            lines.extend(f"   • {err}" for err in self.source_errors[:3])

        # Пустой источник ошибки не даёт, но означает, что он недоступен
        # или сменил вёрстку. Без этой строки поломка была незаметной:
        # в сводке просто оказывалось подозрительно мало новостей.
        if self.empty_sources:
            lines.append("")
            lines.append(f"🫙 Ничего не вернули: {len(self.empty_sources)}")
            lines.extend(f"   • {name}" for name in self.empty_sources[:5])
        return lines


# --------------------------------------------------------------------------
# Сбор
# --------------------------------------------------------------------------

def collect_raw_news(stats: CollectionStats | None = None) -> list[RawNewsItem]:
    """
    Собирает новости со всех источников параллельно:

      - официальные ленты МВД (федеральная + территориальные) — главный
        источник: именно там полиция публикует истории о спасениях,
        и там же сразу известен регион;
      - общие ленты СМИ — с keyword-фильтром, они шумные;
      - публичные Telegram-каналы ведомств — без keyword-фильтра,
        они и так тематические.

    Ошибка в одном источнике не валит сбор по остальным: источник просто
    попадает в stats.source_errors и его видно в сводке /fetch.
    """
    stats = stats or CollectionStats()
    mvd_items: list[RawNewsItem] = []
    media_items: list[RawNewsItem] = []
    telegram_items: list[RawNewsItem] = []

    def fetch_rss(url: str, label: str) -> tuple[str, list[RawNewsItem]]:
        return label, RSSSource(url, source_name=label).fetch(
            limit=settings.fetch_per_source
        )

    def fetch_channel(name: str) -> tuple[str, list[RawNewsItem]]:
        return f"@{name}", TelegramChannelSource(name).fetch(
            limit=settings.fetch_per_source
        )

    jobs: list = []
    with ThreadPoolExecutor(max_workers=SOURCE_FETCH_WORKERS) as pool:
        if settings.mvd_rss_enabled:
            for code in settings.mvd_rss_regions:
                region = region_for_code(code) or f"регион {code}"
                jobs.append((
                    pool.submit(fetch_rss, f"https://{code}.мвд.рф/news/rss",
                                f"МВД: {region}"),
                    "mvd",
                ))

        for url in settings.rss_sources:
            jobs.append((pool.submit(fetch_rss, url, RSSSource(url).source_name), "media"))

        for channel in settings.telegram_channels:
            jobs.append((pool.submit(fetch_channel, channel), "telegram"))

        for future, kind in jobs:
            try:
                label, items = future.result(timeout=120)
            except Exception as exc:
                message = f"{kind}: {type(exc).__name__}: {exc}"
                logger.warning("Источник недоступен — %s", message)
                stats.source_errors.append(message)
                continue

            stats.source_counts[label] = len(items)
            if not items:
                # Пустой источник — не ошибка, но и не норма: так
                # выглядит заблокированный или сменивший вёрстку сайт.
                stats.empty_sources.append(label)

            if kind == "mvd":
                mvd_items.extend(items)
            elif kind == "media":
                media_items.extend(items)
            else:
                telegram_items.extend(items)

    media_filtered = filter_by_keywords(media_items, settings.police_keywords)
    logger.info(
        "Собрано: МВД %d, СМИ %d (после keyword-фильтра %d), Telegram %d",
        len(mvd_items), len(media_items), len(media_filtered), len(telegram_items),
    )

    # Telegram-каналы и ленты МВД вперёд: это первоисточники.
    combined = telegram_items + mvd_items + media_filtered
    stats.raw_collected = len(combined)
    stats.mvd_collected = len(mvd_items)
    stats.media_collected = len(media_filtered)
    stats.telegram_collected = len(telegram_items)
    return combined


def _is_too_old(raw: RawNewsItem) -> bool:
    if settings.max_news_age_days <= 0:
        return False
    published = raw.published_at
    if published is None:
        return False
    # Приводим к aware-времени: RSS и t.me отдают разные форматы, и
    # сравнение naive с aware бросало TypeError прямо посреди сбора.
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_news_age_days)
    return published < cutoff


# --------------------------------------------------------------------------
# Обработка одной новости
# --------------------------------------------------------------------------

# Короче этого текст новости считается «только заголовком»: ленты МВД
# отдают в RSS пустой summary, и без догрузки страницы и модель, и
# дедупликация работали по одной строке.
MIN_TEXT_FOR_LLM = 200


def enrich_from_page(raw: RawNewsItem) -> RawNewsItem:
    """
    Догружает со страницы новости то, чего не было в ленте: полный
    текст, снимки в оригинальном размере и видео.

    Вызывается ТОЛЬКО для отобранных кандидатов, уже после сортировки:
    делать запрос за каждой из полутора тысяч новостей было бы долго и
    невежливо по отношению к сайтам источников.
    """
    needs_text = len(raw.raw_text or "") < MIN_TEXT_FOR_LLM
    if not needs_text and raw.image_url and raw.video_url:
        return raw

    media = extract_from_page(raw.source_url)

    if needs_text and media.text:
        raw.raw_text = media.text
    if not raw.image_url and media.images:
        raw.image_url = media.images[0]
    if not raw.video_url and media.video:
        raw.video_url = media.video

    return raw


def process_raw_item(raw: RawNewsItem, stats: CollectionStats | None = None) -> NewsItem | None:
    """
    Прогоняет одну сырую новость: верификация -> рерайт -> изображение.
    Возвращает None, если новость отклонена (и сохраняет это в БД для
    истории, чтобы не обрабатывать её повторно на следующем цикле).
    """
    stats = stats or CollectionStats()

    if db.item_exists(settings.db_path, raw.source_url):
        stats.duplicates += 1
        return None
    if db.similar_item_exists(settings.db_path, raw.raw_text):
        stats.duplicates += 1
        return None
    if _is_too_old(raw):
        stats.too_old += 1
        return None

    # Догружаем страницу ДО проверки моделью: иначе она судит о новости
    # по одному заголовку, а рерайт пересказывает заголовок заголовком.
    raw = enrich_from_page(raw)

    # Дубликат мог не определиться по пустому тексту из ленты — после
    # догрузки проверяем ещё раз: одна и та же новость приходит и в
    # федеральную ленту МВД, и в региональную.
    if db.similar_item_exists(settings.db_path, raw.raw_text):
        stats.duplicates += 1
        return None

    item = NewsItem(
        id=str(uuid.uuid4()),
        source_name=raw.source_name,
        source_url=raw.source_url,
        title=raw.title,
        raw_text=raw.raw_text,
        published_at=raw.published_at,
    )

    verification = verify_news_item(item)
    if not verification.passed:
        item.status = NewsStatus.REJECTED
        item.rejection_reason = verification.notes
        item.reject_code = verification.reject_code
        item.region = verification.region_hint
        item.city = verification.city_hint
        db.save_item(settings.db_path, item)

        stats.rejected += 1
        code = verification.reject_code or "llm"
        stats.reject_breakdown[code] = stats.reject_breakdown.get(code, 0) + 1
        logger.debug("Отклонено (%s): %s", code, (raw.title or "")[:80])
        return None

    item.status = NewsStatus.VERIFIED
    item.region = verification.region_hint
    item.city = verification.city_hint
    item.verification_notes = verification.notes
    item.tags = verification.tags

    # ВАЖЕН ПОРЯДОК: сначала чистый текст рерайта, потом изображение
    # (оно строится по этому тексту), и только в самом конце — подпись
    # канала. Иначе футер со ссылками попадает и в промпт, и на карточку.
    item.rewritten_text = rewrite_news_item(item)
    item.status = NewsStatus.REWRITTEN

    # Видео важнее фотографии: ролик со спасением показывает событие
    # целиком, тогда как кадр из него — лишь момент. Картинку всё равно
    # готовим: она пойдёт в превью и пригодится, если видео не дойдёт.
    item.video_path = download_video(raw.video_url)

    image_result = get_image_for_item(item, raw_image_url=raw.image_url)
    item.image_path = image_result.path
    item.image_source = image_result.source
    if image_result.notes:
        logger.info("Изображение (%s): %s", image_result.source, image_result.notes)

    item.rewritten_text = append_footer_if_missing(item.rewritten_text)

    item.status = NewsStatus.PENDING_MODERATION
    db.save_item(settings.db_path, item)

    stats.accepted += 1
    return item


# --------------------------------------------------------------------------
# Цикл сбора
# --------------------------------------------------------------------------

def run_collection_cycle(
    limit: int | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[list[NewsItem], CollectionStats]:
    """
    Собирает новости и останавливается, когда найдено `limit` подходящих
    постов. Возвращает (готовые посты, статистика цикла).
    """
    limit = limit or settings.fetch_limit
    stats = CollectionStats()

    db.init_db(settings.db_path)

    if progress:
        progress("Опрашиваю источники...")

    raw_items = collect_raw_news(stats)
    logger.info("Собрано сырых новостей: %d", len(raw_items))

    # Сортируем по эвристике: спасения и помощь людям проверяются
    # моделью первыми, служебные сводки — в последнюю очередь. Без этого
    # лимит проверок уходил бы на отчёты о наркопритонах, а хорошие
    # истории не успевали до него добраться.
    raw_items = rank_candidates(raw_items)

    if progress:
        progress(f"Собрано {len(raw_items)} записей, отбираю лучшие...")

    pending: list[NewsItem] = []
    llm_calls = 0

    for index, raw in enumerate(raw_items, 1):
        if len(pending) >= limit:
            logger.info("Достигнут лимит в %d постов — останавливаю обработку.", limit)
            break

        if llm_calls >= settings.max_llm_checks:
            logger.info(
                "Исчерпан лимит проверок моделью (%d) — остальные новости "
                "останутся до следующего цикла.", settings.max_llm_checks,
            )
            break

        try:
            before = stats.rejected + stats.accepted
            processed = process_raw_item(raw, stats)
            # Пауза нужна только если реально ходили в LLM: дубликаты
            # отсеиваются локально и тормозить на них незачем.
            if stats.rejected + stats.accepted > before:
                llm_calls += 1
                time.sleep(PAUSE_BETWEEN_LLM_CALLS)
        except Exception:
            logger.exception("Ошибка обработки новости из %s", raw.source_name)
            continue

        stats.llm_checks = llm_calls

        if processed:
            pending.append(processed)
            if progress:
                progress(
                    f"Готово {len(pending)} из {limit} "
                    f"(проверено {index} из {len(raw_items)})"
                )

    logger.info(
        "Цикл завершён: собрано=%d, дубликатов=%d, отклонено=%d, принято=%d, "
        "вызовов LLM=%d, %.0f сек.",
        stats.raw_collected, stats.duplicates, stats.rejected,
        stats.accepted, llm_calls, stats.duration_seconds,
    )
    return pending, stats


def regenerate_image(item_id: str, force_card: bool = True) -> NewsItem | None:
    """
    Перевыпускает изображение для уже собранной новости — используется
    кнопкой «Другая картинка» в чате модераторов, когда фото из источника
    не подошло (например, на нём коллаж с текстом или чужой водяной знак).
    """
    item = db.get_by_id(settings.db_path, item_id)
    if item is None:
        return None

    result = get_image_for_item(
        item, raw_image_url=None, allow_original=False, force_card=force_card
    )
    db.set_image(settings.db_path, item_id, result.path, result.source)

    item.image_path = result.path
    item.image_source = result.source
    return item
