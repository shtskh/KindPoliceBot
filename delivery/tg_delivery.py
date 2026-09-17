"""
Доставка постов в Telegram.

Текст приходит уже как HTML (см. pipeline/rewrite.py — разрешены только
<b>/<i> — и pipeline/post_formatting.py — обязательная подпись со
ссылками), поэтому отправляем с parse_mode="HTML".

Главное отличие от первой версии: ошибка публикации больше не молчит.
Раньше код ловил RequestException и печатал только его текст — а Telegram
на неудачу отвечает HTTP 400 с полем description («can't parse entities»,
«chat not found», «PHOTO_INVALID_DIMENSIONS»), которое как раз и
объясняет причину. Тело ответа терялось, и в логах оставалось
бесполезное «400 Client Error». Теперь description логируется и
возвращается вызывающему коду, а при ошибке разметки пост
переотправляется без форматирования — лучше пост без курсива, чем
несостоявшаяся публикация.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import requests

from delivery.base import DeliveryChannel
from logging_setup import get_logger
from paths import resolve

logger = get_logger("delivery.telegram")

# Лимит Telegram на подпись к фото — 1024 символа
# (для обычного текстового сообщения — 4096).
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

_TAG_PATTERN = re.compile(r"<[^>]+>")


@dataclass
class DeliveryResult:
    ok: bool
    message_id: int | None = None
    error: str = ""

    def __bool__(self) -> bool:  # чтобы `if delivery.send_post(...)` работал
        return self.ok


def strip_html(text: str) -> str:
    return _TAG_PATTERN.sub("", text or "")


def _safe_truncate_html(text: str, limit: int) -> str:
    """
    Если текст (уже содержащий теги <b>/<i>/<a>) превышает лимит, обрезка
    по символам рискует разорвать тег или оставить его открытым, из-за
    чего Telegram отклонит сообщение целиком. Поэтому при превышении
    лимита убираем разметку и обрезаем как обычный текст — лучше без
    форматирования, чем не отправить пост.
    """
    if len(text) <= limit:
        return text

    plain = strip_html(text)
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1].rstrip() + "…"


class TelegramDelivery(DeliveryChannel):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base = f"https://api.telegram.org/bot{bot_token}"

    # -- низкоуровневый вызов -------------------------------------------------

    def _call(self, method: str, data: dict, files: dict | None = None) -> DeliveryResult:
        try:
            response = requests.post(
                f"{self.api_base}/{method}",
                data=data,
                files=files,
                timeout=60 if files else 20,
            )
        except requests.RequestException as exc:
            error = f"сеть: {type(exc).__name__}: {exc}"
            logger.error("[%s] %s", method, error)
            return DeliveryResult(ok=False, error=error)

        try:
            payload = response.json()
        except ValueError:
            error = f"HTTP {response.status_code}, ответ не JSON: {response.text[:200]}"
            logger.error("[%s] %s", method, error)
            return DeliveryResult(ok=False, error=error)

        if payload.get("ok"):
            return DeliveryResult(
                ok=True, message_id=(payload.get("result") or {}).get("message_id")
            )

        description = payload.get("description", "без описания")
        error = f"Telegram отказал: {description} (HTTP {response.status_code})"
        logger.error("[%s] %s", method, error)
        return DeliveryResult(ok=False, error=error)

    # -- публичный интерфейс --------------------------------------------------

    def send_post_detailed(
        self, text: str, image_path: str | None = None
    ) -> DeliveryResult:
        """
        Отправляет пост и возвращает подробный результат (id сообщения или
        текст ошибки) — бот показывает его модератору вместо «см. логи».
        """
        text = text or ""
        resolved_image: Path | None = None

        if image_path:
            candidate = resolve(image_path)
            if candidate.is_file():
                resolved_image = candidate
            else:
                logger.warning(
                    "Файл изображения не найден (%s) — отправляю пост текстом.",
                    candidate,
                )

        if resolved_image is not None:
            result = self._send_photo(text, resolved_image)
            if result.ok:
                return result
            # Картинка не прошла (битый файл, неподходящие размеры) —
            # публикуем хотя бы текст, чтобы новость не потерялась.
            logger.warning("Не удалось отправить фото, публикую текстом: %s", result.error)

        return self._send_text(text)

    def _send_photo(self, text: str, image: Path) -> DeliveryResult:
        caption = _safe_truncate_html(text, CAPTION_LIMIT)
        try:
            with open(image, "rb") as photo:
                result = self._call(
                    "sendPhoto",
                    {
                        "chat_id": self.chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    files={"photo": photo},
                )
        except OSError as exc:
            return DeliveryResult(ok=False, error=f"не удалось прочитать файл: {exc}")

        if result.ok or "parse" not in result.error.lower():
            return result

        # Разметка не понравилась Telegram — пробуем без неё.
        logger.warning("Проблема с HTML-разметкой, повторяю без форматирования.")
        try:
            with open(image, "rb") as photo:
                return self._call(
                    "sendPhoto",
                    {
                        "chat_id": self.chat_id,
                        "caption": _safe_truncate_html(strip_html(text), CAPTION_LIMIT),
                    },
                    files={"photo": photo},
                )
        except OSError as exc:
            return DeliveryResult(ok=False, error=f"не удалось прочитать файл: {exc}")

    def _send_text(self, text: str) -> DeliveryResult:
        body = _safe_truncate_html(text, MESSAGE_LIMIT)
        if not body.strip():
            return DeliveryResult(ok=False, error="пустой текст поста")

        result = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        if result.ok or "parse" not in result.error.lower():
            return result

        logger.warning("Проблема с HTML-разметкой, повторяю без форматирования.")
        return self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": _safe_truncate_html(strip_html(text), MESSAGE_LIMIT),
                "disable_web_page_preview": "true",
            },
        )

    def send_post(self, text: str, image_path: str | None = None) -> bool:
        """Совместимый со старым кодом булев интерфейс."""
        return self.send_post_detailed(text, image_path).ok
