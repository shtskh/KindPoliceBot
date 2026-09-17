"""
Доставка постов в MAX (max.ru).

Требует токена бота, полученного либо через @MasterBot (/create — для
тестового бота), либо через полноценную верификацию юрлица для боевого
бота. Используется официальный REST API: https://dev.max.ru/docs-api

ОСОБЕННОСТИ API MAX, из-за которых код отличается от телеграмного:

  1. Авторизация идёт параметром запроса `access_token`, а не заголовком
     Authorization. Прежняя версия слала токен как
     `Authorization: <token>` — MAX такой запрос не принимает.
  2. Адресат указывается параметром `chat_id` в query string, а тело
     запроса содержит только текст и вложения.
  3. Загрузка картинки двухшаговая: сначала POST /uploads?type=image
     отдаёт временный URL, туда кладётся файл, и уже полученный токен
     вложения прикрепляется к сообщению.

Интерфейс send_post() совместим с TelegramDelivery, поэтому
оркестратору и боту всё равно, в какой канал уходит пост.

СТАТУС: код написан по документации, но НЕ проверен на живом токене —
у проекта его пока нет. Все обращения к сети обёрнуты так, чтобы сбой
MAX не мешал публикации в Telegram. Первое, что нужно сделать при
получении токена, — вызвать check_connection().
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from delivery.base import DeliveryChannel
from logging_setup import get_logger
from paths import resolve

logger = get_logger("delivery.max")

MAX_API_BASE = "https://botapi.max.ru"

# У MAX лимит на длину текста сообщения — 4000 символов.
MESSAGE_LIMIT = 4000


@dataclass
class MaxResult:
    ok: bool
    message_id: str | None = None
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok


class MaxDelivery(DeliveryChannel):
    def __init__(self, bot_token: str, chat_id: str, api_base: str = MAX_API_BASE):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base = api_base.rstrip("/")

    # -- служебное ------------------------------------------------------------

    def _params(self, **extra) -> dict:
        params = {"access_token": self.bot_token}
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _describe_error(self, response: requests.Response) -> str:
        try:
            payload = response.json()
            detail = payload.get("message") or payload.get("error") or str(payload)
        except ValueError:
            detail = response.text[:200]
        return f"HTTP {response.status_code}: {detail}"

    # -- проверка связи -------------------------------------------------------

    def check_connection(self) -> MaxResult:
        """
        Проверяет токен через GET /me. Вызвать сразу после получения
        боевого токена — так ошибка конфигурации всплывёт до того, как
        от неё пострадает публикация.
        """
        try:
            response = requests.get(
                f"{self.api_base}/me", params=self._params(), timeout=15
            )
        except requests.RequestException as exc:
            return MaxResult(ok=False, error=f"сеть: {type(exc).__name__}: {exc}")

        if response.status_code != 200:
            return MaxResult(ok=False, error=self._describe_error(response))

        try:
            name = response.json().get("name", "без имени")
        except ValueError:
            name = "неизвестно"
        logger.info("MAX: подключён бот «%s»", name)
        return MaxResult(ok=True)

    # -- загрузка изображения -------------------------------------------------

    def _upload_image(self, image: Path) -> dict | None:
        """
        Двухшаговая загрузка: получить временный URL, отправить туда файл.
        Возвращает готовое описание вложения или None, если не вышло —
        тогда пост уйдёт просто текстом.
        """
        try:
            response = requests.post(
                f"{self.api_base}/uploads",
                params=self._params(type="image"),
                timeout=20,
            )
            if response.status_code != 200:
                logger.warning("MAX: не удалось получить URL загрузки — %s",
                               self._describe_error(response))
                return None

            upload_url = response.json().get("url")
            if not upload_url:
                logger.warning("MAX: ответ на /uploads без поля url")
                return None

            with open(image, "rb") as handle:
                uploaded = requests.post(
                    upload_url, files={"data": handle}, timeout=120
                )
            if uploaded.status_code != 200:
                logger.warning("MAX: загрузка файла не удалась — %s",
                               self._describe_error(uploaded))
                return None

            payload = uploaded.json()
            # MAX отдаёт либо {"photos": {...}}, либо {"token": "..."} —
            # зависит от версии API, поддерживаем оба варианта.
            if "photos" in payload:
                return {"type": "image", "payload": payload["photos"]}
            if payload.get("token"):
                return {"type": "image", "payload": {"token": payload["token"]}}

            logger.warning("MAX: непонятный ответ загрузки: %s", str(payload)[:200])
            return None

        except (requests.RequestException, ValueError, OSError) as exc:
            logger.warning("MAX: ошибка загрузки изображения: %s", exc)
            return None

    # -- отправка -------------------------------------------------------------

    def send_post_detailed(self, text: str, image_path: str | None = None) -> MaxResult:
        if not self.bot_token:
            return MaxResult(ok=False, error="MAX_BOT_TOKEN не задан")
        if not self.chat_id:
            return MaxResult(ok=False, error="MAX_PUBLISH_CHAT_ID не задан")

        # MAX не поддерживает HTML-разметку Telegram — отправляем чистый
        # текст, иначе в посте будут видны сами теги <b> и <i>.
        from delivery.tg_delivery import strip_html

        body = strip_html(text or "").strip()
        if len(body) > MESSAGE_LIMIT:
            body = body[: MESSAGE_LIMIT - 1].rstrip() + "…"
        if not body:
            return MaxResult(ok=False, error="пустой текст поста")

        payload: dict = {"text": body}

        if image_path:
            image = resolve(image_path)
            if image.is_file():
                attachment = self._upload_image(image)
                if attachment:
                    payload["attachments"] = [attachment]
            else:
                logger.warning("MAX: файл изображения не найден: %s", image)

        try:
            response = requests.post(
                f"{self.api_base}/messages",
                params=self._params(chat_id=self.chat_id),
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            error = f"сеть: {type(exc).__name__}: {exc}"
            logger.error("MAX: %s", error)
            return MaxResult(ok=False, error=error)

        if response.status_code != 200:
            error = self._describe_error(response)
            logger.error("MAX: отправка не удалась — %s", error)
            return MaxResult(ok=False, error=error)

        try:
            message_id = (
                response.json().get("message", {}).get("body", {}).get("mid")
            )
        except (ValueError, AttributeError):
            message_id = None

        logger.info("MAX: пост опубликован (mid=%s)", message_id)
        return MaxResult(ok=True, message_id=message_id)

    def send_post(self, text: str, image_path: str | None = None) -> bool:
        return self.send_post_detailed(text, image_path).ok
