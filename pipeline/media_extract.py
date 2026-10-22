"""
Извлечение фото и видео со страницы новости.

ЗАЧЕМ
-----
1. КАЧЕСТВО ФОТО. В лентах МВД (`NN.мвд.рф/news/rss`) картинок нет
   вовсе, а на странице новости снимки лежат уменьшенными копиями вида
   `IMG_1234-800x600.jpg`. Оригинал доступен по тому же адресу без
   суффикса размера: проверено — 1280×960 вместо 800×600. Раньше бот
   таких новостей вообще не видел с фото и рисовал карточку.

2. ВИДЕО. В официальных Telegram-каналах ведомств (например,
   @IrinaVolk_MVD) значительная часть постов — короткие ролики со
   спасениями. Ссылка на mp4 есть прямо в разметке публичной страницы
   канала. На сайтах МВД видео встречается реже, но тоже бывает.

ЧТО ВАЖНО ПРИ ОТБОРЕ КАРТИНОК
-----------------------------
На странице МВД полно служебной графики: гербы, эмблемы управлений,
иконки интерфейса. Если брать первую попавшуюся картинку, в пост уйдёт
герб вместо фотографии с места события. Поэтому берутся только снимки
из папок с контентом, а эмблемы отсеиваются по именам файлов.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

from logging_setup import get_logger

logger = get_logger("media")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

PAGE_TIMEOUT = 20

# Снимки на сайтах МВД лежат в хранилище static.mvd.ru. Нас интересуют
# папки с содержимым новости, а не оформление сайта.
_MVD_IMAGE_RE = re.compile(
    r"""['"(]((?:https?:)?//static\.mvd\.ru/upload/[^'")\s]+?\.(?:jpe?g|png))""",
    re.IGNORECASE,
)

# Служебная графика: гербы, эмблемы, логотипы, иконки, подложки.
_DECOR_WORDS = (
    "emblem", "logo", "coat", "geraldika", "geraldic", "icon", "favicon",
    "banner", "bg_", "background", "placeholder", "sprite", "watermark",
    "gerb", "simvol", "znak",
)

# Папки, где лежит именно содержимое новости.
_CONTENT_HINTS = ("document_images", "news", "content", "photo", "gallery")

# Суффикс уменьшенной копии перед расширением: `-800x600`, а на части
# региональных сайтов — `-800xx600` с удвоенной «x» (вариант обрезки).
# Без учёта второго варианта один и тот же снимок попадал в список
# дважды: как оригинал и как уменьшенная копия.
_THUMB_SUFFIX_RE = re.compile(r"-\d{2,4}x{1,2}\d{2,4}(?=\.[a-z]{3,4}$)", re.IGNORECASE)

_VIDEO_RE = re.compile(
    r"""['"(]((?:https?:)?//[^'")\s]+?\.(?:mp4|webm))""", re.IGNORECASE
)

_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
_OG_VIDEO_RE = re.compile(
    r"""<meta[^>]+property=["']og:video(?::url)?["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)


@dataclass
class PageMedia:
    images: list[str] = field(default_factory=list)
    video: str | None = None
    # Текст новости со страницы. В лентах МВД его нет вообще: RSS
    # отдаёт только заголовок, и без этого поля и модель, и
    # дедупликация работали по одной строке заголовка.
    text: str = ""

    @property
    def best_image(self) -> str | None:
        return self.images[0] if self.images else None


def _absolute(url: str) -> str:
    """`//static.mvd.ru/...` -> `https://static.mvd.ru/...`"""
    if url.startswith("//"):
        return "https:" + url
    return url


def upgrade_image_url(url: str) -> str:
    """
    Превращает адрес уменьшенной копии в адрес оригинала.

    `IMG_123-800x600.jpg` -> `IMG_123.jpg`. Если суффикса размера нет,
    адрес возвращается без изменений.
    """
    return _THUMB_SUFFIX_RE.sub("", url)


def _is_decorative(url: str) -> bool:
    lowered = url.lower()
    return any(word in lowered for word in _DECOR_WORDS)


def _looks_like_content(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in _CONTENT_HINTS)


# Где на сайтах МВД лежит текст новости. Порядок — от точного к общему.
_ARTICLE_SELECTORS = (
    "div.article",
    "div.news-detail",
    "div[itemprop='articleBody']",
    "article",
    "div.content-text",
)

# Хвосты страницы, которые не относятся к самой новости.
_TEXT_JUNK_RE = re.compile(
    r"(Поделиться|Версия для печати|Дата публикации|Напечатать|"
    r"Все новости|Вернуться к списку)[\s\S]*$",
    re.IGNORECASE,
)


def _extract_article_text(html: str) -> str:
    """Текст новости со страницы — то, чего нет в RSS МВД."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""

    for selector in _ARTICLE_SELECTORS:
        try:
            element = soup.select_one(selector)
        except Exception:
            continue
        if element is None:
            continue

        text = element.get_text(" ", strip=True)
        text = _TEXT_JUNK_RE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 120:
            return text

    return ""


def extract_from_page(page_url: str, session: requests.Session | None = None) -> PageMedia:
    """
    Скачивает страницу новости и достаёт из неё фото и видео.

    Никогда не бросает исключение: при любой проблеме возвращает пустой
    результат, и пайплайн просто обходится карточкой, как раньше.
    """
    if not page_url or not page_url.startswith(("http://", "https://")):
        return PageMedia()

    getter = session.get if session else requests.get
    try:
        response = getter(
            page_url, headers={"User-Agent": USER_AGENT}, timeout=PAGE_TIMEOUT
        )
        if response.status_code != 200:
            logger.debug("Страница %s: HTTP %s", page_url[:80], response.status_code)
            return PageMedia()
        html = response.text
    except requests.RequestException as exc:
        logger.debug("Страница %s недоступна: %s", page_url[:80], exc)
        return PageMedia()

    media = PageMedia()
    media.text = _extract_article_text(html)

    # --- фотографии ---
    candidates = [_absolute(u) for u in _MVD_IMAGE_RE.findall(html)]
    content = [u for u in candidates if _looks_like_content(u) and not _is_decorative(u)]

    # Если папки с содержимым не нашлось, берём любые неслужебные снимки:
    # вёрстка у региональных сайтов различается.
    if not content:
        content = [u for u in candidates if not _is_decorative(u)]

    seen: set[str] = set()
    for url in content:
        original = upgrade_image_url(url)
        if original not in seen:
            seen.add(original)
            media.images.append(original)

    if not media.images:
        og_image = _OG_IMAGE_RE.search(html)
        if og_image and not _is_decorative(og_image.group(1)):
            media.images.append(_absolute(upgrade_image_url(og_image.group(1))))

    # --- видео ---
    og_video = _OG_VIDEO_RE.search(html)
    if og_video:
        media.video = _absolute(og_video.group(1))
    else:
        for url in _VIDEO_RE.findall(html):
            absolute = _absolute(url)
            if not _is_decorative(absolute):
                media.video = absolute
                break

    if media.images or media.video:
        logger.debug(
            "Со страницы %s: фото %d, видео %s",
            page_url[:60], len(media.images), "есть" if media.video else "нет",
        )
    return media
