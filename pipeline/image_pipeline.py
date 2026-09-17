"""
Этап «Изображение».

Порядок попыток (сверху вниз, до первого успеха):
  1. Реальное фото из источника — самый честный вариант для новости.
  2. Удалённая генерация через AI Provider (/images/generations).
  3. Локальная фирменная карточка (pipeline/image_render.py) — рисуется
     всегда и не зависит от сети.

ДИАГНОЗ ПО ПУНКТУ 2 (почему «генерация не работала»)
----------------------------------------------------
Боевой провайдер aiprovider.duckdns.org НЕ поддерживает генерацию
изображений: POST /v1/images/generations отвечает 404 "Endpoint not
found" при любой модели, а в /v1/models нет ни одной image-модели.
Старый код при этом:
  * ретраил 404 как временную ошибку (то есть три раза ждал впустую);
  * терял тело ответа, поэтому в логах не было видно причины;
  * не имел запасного варианта, и пост уходил вообще без картинки.

Теперь 404/401/403 распознаются как постоянный отказ: генерация
помечается недоступной на весь запуск процесса (чтобы не долбить
провайдера впустую на каждой новости), а картинку рисует локальный
рендер. Как только у провайдера появится image-эндпоинт, удалённая
генерация включится сама — код и настройки уже готовы.

ЮРИДИЧЕСКИЕ ЗАМЕЧАНИЯ (важно перед боевым запуском)
---------------------------------------------------
  1. Перед использованием чужого фото нужно проверить лицензию у
     источника — не все новостные фото свободны для переиспользования.
  2. Сгенерированное изображение нельзя выдавать за фотографию с места
     события. Локальная карточка тем и хороша, что очевидно является
     оформлением текста, а не «фотодоказательством».
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import re
from dataclasses import dataclass

import requests

from config import settings
from logging_setup import get_logger
from paths import IMAGES_DIR, ensure_dirs, resolve, to_relative
from pipeline.ai_client import AIProviderError, post_json
from pipeline.image_render import CARD_RENDER_AVAILABLE, render_news_card
from storage.models import NewsItem

logger = get_logger("image")

# Отсекаем совсем маленькие/битые картинки (иконки, заглушки, "1x1" пиксели).
MIN_IMAGE_BYTES = 15_000
# Telegram отклоняет фото больше 10 МБ — пережимаем заранее, чтобы не
# получить 400 уже на этапе публикации.
MAX_IMAGE_BYTES = 9_000_000
# Не скачиваем гигантские файлы (баннер-видео, PSD и прочие сюрпризы).
MAX_DOWNLOAD_BYTES = 25_000_000
# Ограничения Telegram на пропорции и суммарный размер сторон.
MAX_TELEGRAM_DIMENSION_SUM = 10_000
MAX_TELEGRAM_RATIO = 20

DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

IMAGE_GENERATION_PROMPT_TEMPLATE = """Documentary-style realistic photograph:
a Russian police officer in regulation uniform (accurate insignia, no distortions),
scene: {scene_description}.
Neutral, calm, professional framing. No close-up recognizable faces,
no text, no logos, no watermarks on the image."""

# Признаки того, что провайдер отдал HTML-страницу ошибки вместо картинки.
_HTML_SNIFF = (b"<!doctype", b"<html", b"<?xml")

# Магические байты поддерживаемых форматов -> расширение файла.
_MAGIC_EXTENSIONS = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # уточняется ниже по сигнатуре WEBP
)

# Один раз за запуск процесса выясняем, есть ли у провайдера генерация.
# None = ещё не проверяли, False = эндпоинта нет (больше не пробуем).
_remote_generation_available: bool | None = None
_remote_generation_reason: str = ""


@dataclass
class ImageResult:
    path: str | None
    source: str  # "original" | "generated" | "card" | "none"
    notes: str = ""


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def get_image_for_item(
    item: NewsItem,
    raw_image_url: str | None,
    *,
    allow_original: bool = True,
    force_card: bool = False,
) -> ImageResult:
    """
    Подбирает изображение для новости.

    allow_original=False и force_card=True нужны команде /regen_image в
    боте: модератор может попросить перерисовать карточку, если фото из
    источника не подошло.
    """
    ensure_dirs()

    if force_card:
        return _render_card(item, notes="Принудительный рендер карточки")

    if allow_original and raw_image_url:
        downloaded = _try_download(raw_image_url)
        if downloaded:
            return ImageResult(path=downloaded, source="original")
        logger.info("Фото из источника не подошло, пробую другие варианты.")

    scene = _scene_text(item)
    if not scene:
        return ImageResult(
            path=None, source="none", notes="Нет текста для составления промпта."
        )

    if settings.image_generation_enabled:
        generated, error = _generate_image_remote(scene)
        if generated:
            return ImageResult(path=generated, source="generated")
        if error:
            logger.info("Удалённая генерация недоступна: %s", error)

    if settings.image_card_fallback:
        return _render_card(item)

    return ImageResult(path=None, source="none", notes="Изображение не получено.")


def _render_card(item: NewsItem, notes: str = "") -> ImageResult:
    if not CARD_RENDER_AVAILABLE:
        return ImageResult(
            path=None, source="none",
            notes="Pillow не установлен — карточку нарисовать нечем.",
        )

    path = render_news_card(
        _scene_text(item),
        region=item.region,
        city=item.city,
        published_at=item.published_at,
        channel_title=settings.channel_title,
        source_name=item.source_name,
    )
    if path:
        return ImageResult(path=to_relative(path), source="card", notes=notes)
    return ImageResult(path=None, source="none", notes="Не удалось нарисовать карточку.")


# --------------------------------------------------------------------------
# Подготовка текста для промпта/карточки
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _scene_text(item: NewsItem) -> str:
    """
    Чистый текст новости без разметки и без обязательной подписи канала.

    БЫЛ БАГ: сюда попадал item.rewritten_text уже вместе с футером
    (`​📌 <a href="https://t.me/kindpolice">Хороший полицейский</a> | …`),
    потому что orchestrator дописывал подпись ДО вызова этого модуля.
    В результате и промпт генерации, и текст на карточке содержали
    HTML-теги и рекламу канала вместо сути события.
    """
    # FOOTER_MARKER импортируем локально, чтобы не создавать цикл импортов
    # (post_formatting -> config -> ... ).
    from pipeline.post_formatting import FOOTER_MARKER

    text = item.rewritten_text or item.title or item.raw_text or ""
    text = text.split(FOOTER_MARKER)[0]
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# Скачивание оригинального фото
# --------------------------------------------------------------------------

def _try_download(url: str) -> str | None:
    """
    Скачивает фото из источника и нормализует его под требования Telegram.

    БЫЛ БАГ: имя файла считалось как abs(hash(url)). Встроенный hash()
    для строк рандомизируется при каждом запуске процесса (PYTHONHASHSEED),
    поэтому одна и та же картинка каждый раз сохранялась под новым именем —
    кеша не было, а папка storage/images росла бесконечно. Теперь имя —
    это sha256 от URL, то есть стабильное между запусками.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]

    # Уже скачивали раньше — не ходим в сеть повторно.
    for existing in IMAGES_DIR.glob(f"src_{digest}.*"):
        if existing.stat().st_size >= MIN_IMAGE_BYTES:
            logger.debug("Фото уже в кеше: %s", existing.name)
            return to_relative(existing)

    try:
        response = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": DOWNLOAD_USER_AGENT, "Accept": "image/*,*/*"},
            stream=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and not content_type.startswith("image/"):
            logger.info("По ссылке не изображение (Content-Type: %s): %s",
                        content_type, url[:120])
            return None

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                logger.info("Файл слишком большой (>%d байт), пропускаю: %s",
                            MAX_DOWNLOAD_BYTES, url[:120])
                return None
        content = b"".join(chunks)

    except requests.RequestException as exc:
        logger.info("Не удалось скачать фото %s: %s", url[:120], exc)
        return None

    return _store_image_bytes(content, prefix=f"src_{digest}")


# --------------------------------------------------------------------------
# Удалённая генерация
# --------------------------------------------------------------------------

def _generate_image_remote(scene: str) -> tuple[str | None, str | None]:
    """
    Пытается сгенерировать картинку через AI Provider.
    Возвращает (путь, текст_ошибки) — ровно одно из двух не None.
    """
    global _remote_generation_available, _remote_generation_reason

    if _remote_generation_available is False:
        return None, _remote_generation_reason

    if not settings.ai_configured:
        return None, "AI_PROVIDER_API_KEY не задан"

    prompt = IMAGE_GENERATION_PROMPT_TEMPLATE.format(scene_description=scene[:400])
    last_error = "не удалось сгенерировать изображение"

    for model in settings.ai_image_models:
        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": settings.ai_image_size,
            "response_format": "b64_json",
        }

        try:
            data = post_json(
                settings.ai_image_endpoint,
                payload,
                label=f"image:{model}",
                # Генерация долгая, но и ретраить её дорого.
                timeout=180.0,
                max_retries=2,
            )
        except AIProviderError as exc:
            last_error = str(exc)

            if exc.is_permanent and exc.status_code in (404, 405):
                # Эндпоинта нет вовсе — дальше пробовать другие модели
                # бессмысленно, и на следующих новостях тоже.
                _remote_generation_available = False
                _remote_generation_reason = (
                    f"у провайдера нет эндпоинта {settings.ai_image_endpoint} "
                    f"(HTTP {exc.status_code}). Используется локальная карточка."
                )
                logger.warning(
                    "Генерация изображений отключена на этот запуск: %s",
                    _remote_generation_reason,
                )
                return None, _remote_generation_reason

            if exc.is_permanent:
                # Скорее всего эта модель недоступна — пробуем следующую.
                logger.info("Модель %s не подошла: %s", model, exc)
                continue

            return None, last_error

        image_bytes = _extract_image_bytes(data)
        if not image_bytes:
            last_error = f"модель {model} не вернула данных изображения"
            logger.info(last_error)
            continue

        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:20]
        stored = _store_image_bytes(image_bytes, prefix=f"gen_{digest}")
        if stored:
            _remote_generation_available = True
            logger.info("Изображение сгенерировано моделью %s", model)
            return stored, None

        last_error = f"модель {model} вернула некорректное изображение"

    # Ни одна модель не сработала, но эндпоинт существует — не отключаем
    # генерацию навсегда, возможно, дело в конкретном промпте.
    return None, last_error


def _extract_image_bytes(data: dict) -> bytes | None:
    """
    Провайдеры OpenAI-совместимого /images/generations отдают результат
    по-разному: base64 в b64_json, ссылку в url, иногда data:-URI.
    Поддерживаем все три варианта.
    """
    entries = data.get("data") or []
    if not entries:
        return None

    entry = entries[0]
    if not isinstance(entry, dict):
        return None

    b64 = entry.get("b64_json")
    if b64:
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            logger.warning("Не удалось декодировать b64_json: %s", exc)
            return None

    url = entry.get("url")
    if url:
        if url.startswith("data:"):
            try:
                return base64.b64decode(url.split(",", 1)[1])
            except (ValueError, IndexError):
                return None
        try:
            response = requests.get(
                url, timeout=60, headers={"User-Agent": DOWNLOAD_USER_AGENT}
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            logger.warning("Не удалось скачать сгенерированное изображение: %s", exc)
            return None

    return None


# --------------------------------------------------------------------------
# Сохранение и нормализация
# --------------------------------------------------------------------------

def _sniff_extension(content: bytes) -> str | None:
    """Определяет формат по сигнатуре файла, а не по расширению в URL —
    сервер вполне может отдать PNG по ссылке, оканчивающейся на .jpg."""
    for magic, extension in _MAGIC_EXTENSIONS:
        if content.startswith(magic):
            if magic == b"RIFF":
                return ".webp" if content[8:12] == b"WEBP" else None
            return extension
    return None


def _store_image_bytes(content: bytes, prefix: str) -> str | None:
    """
    Проверяет, что это действительно изображение, при необходимости
    приводит его к пригодному для Telegram виду и сохраняет на диск.
    Возвращает путь относительно корня проекта или None.
    """
    if not content:
        return None

    if len(content) < MIN_IMAGE_BYTES:
        logger.info("Изображение подозрительно маленькое (%d байт), пропускаю.",
                    len(content))
        return None

    head = content[:64].lstrip().lower()
    if any(head.startswith(marker) for marker in _HTML_SNIFF):
        logger.info("Вместо изображения пришла HTML-страница, пропускаю.")
        return None

    extension = _sniff_extension(content)
    if extension is None:
        logger.info("Неизвестный формат изображения, пропускаю.")
        return None

    content, extension = _normalize_for_telegram(content, extension)
    if content is None:
        return None

    ensure_dirs()
    path = IMAGES_DIR / f"{prefix}{extension}"
    try:
        path.write_bytes(content)
    except OSError as exc:
        logger.warning("Не удалось сохранить изображение %s: %s", path, exc)
        return None

    logger.info("Изображение сохранено: %s (%d КБ)", path.name, len(content) // 1024)
    return to_relative(path)


def _normalize_for_telegram(content: bytes, extension: str) -> tuple[bytes | None, str]:
    """
    Приводит картинку к тому, что Telegram точно примет через sendPhoto:
    ≤10 МБ, сумма сторон ≤10000, соотношение сторон ≤20:1, формат
    JPEG/PNG. WebP и слишком большие файлы пережимаются в JPEG.

    Если Pillow не установлен — ограничиваемся проверкой размера файла:
    лучше отправить как есть, чем не отправить вовсе.
    """
    try:
        from PIL import Image
    except ImportError:
        if len(content) > MAX_IMAGE_BYTES:
            logger.info("Файл больше лимита Telegram и Pillow недоступен — пропускаю.")
            return None, extension
        return content, extension

    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            width, height = image.size
            image_format = (image.format or "").upper()

            if width < 100 or height < 100:
                logger.info("Изображение слишком мелкое (%dx%d), пропускаю.", width, height)
                return None, extension

            ratio = max(width, height) / max(1, min(width, height))
            needs_reencode = (
                image_format not in ("JPEG", "PNG")
                or len(content) > MAX_IMAGE_BYTES
                or width + height > MAX_TELEGRAM_DIMENSION_SUM
            )

            if ratio > MAX_TELEGRAM_RATIO:
                logger.info("Слишком вытянутое изображение (%dx%d), пропускаю.",
                            width, height)
                return None, extension

            if not needs_reencode:
                return content, extension

            converted = image.convert("RGB")
            if width + height > MAX_TELEGRAM_DIMENSION_SUM:
                scale = MAX_TELEGRAM_DIMENSION_SUM / (width + height)
                converted = converted.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.LANCZOS,
                )

            buffer = io.BytesIO()
            quality = 90
            converted.save(buffer, format="JPEG", quality=quality, optimize=True)
            # Понижаем качество, пока не уложимся в лимит Telegram.
            while buffer.tell() > MAX_IMAGE_BYTES and quality > 45:
                quality -= 15
                buffer = io.BytesIO()
                converted.save(buffer, format="JPEG", quality=quality, optimize=True)

            if buffer.tell() > MAX_IMAGE_BYTES:
                logger.info("Не удалось ужать изображение до лимита Telegram.")
                return None, extension

            return buffer.getvalue(), ".jpg"

    except Exception as exc:
        logger.info("Pillow не смог разобрать изображение (%s), пропускаю.", exc)
        return None, extension


# --------------------------------------------------------------------------
# Обслуживание хранилища
# --------------------------------------------------------------------------

def image_exists(path_like: str | None) -> bool:
    """Проверка наличия файла, устойчивая к относительным путям из БД."""
    if not path_like:
        return False
    try:
        return resolve(path_like).is_file()
    except OSError:
        return False


def cleanup_orphan_images(known_paths: set[str], keep_recent_days: int = 3) -> int:
    """
    Удаляет файлы из storage/images, на которые не ссылается ни одна
    запись в БД (например, картинки отклонённых новостей). Свежие файлы
    не трогает — они могут принадлежать новости, которая прямо сейчас
    обрабатывается и ещё не сохранена.

    Возвращает количество удалённых файлов.
    """
    import time

    if not IMAGES_DIR.exists():
        return 0

    known = {resolve(p) for p in known_paths if p}
    cutoff = time.time() - keep_recent_days * 86400
    removed = 0

    for file_path in IMAGES_DIR.iterdir():
        if not file_path.is_file():
            continue
        if file_path.resolve() in known:
            continue
        try:
            if file_path.stat().st_mtime > cutoff:
                continue
            file_path.unlink()
            removed += 1
        except OSError as exc:
            logger.debug("Не удалось удалить %s: %s", file_path, exc)

    if removed:
        logger.info("Удалено неиспользуемых изображений: %d", removed)
    return removed


def generation_status() -> str:
    """Человекочитаемый статус для команды /diag в боте."""
    if not settings.image_generation_enabled:
        return "выключена настройкой IMAGE_GENERATION_ENABLED"
    if _remote_generation_available is False:
        return f"недоступна ({_remote_generation_reason})"
    if _remote_generation_available is True:
        return "работает"
    return "ещё не проверялась в этом запуске"
