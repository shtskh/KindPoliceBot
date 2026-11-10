"""
Единый HTTP-клиент к AI Provider (OpenAI-совместимый API).

Раньше одинаковый цикл ретраев был скопирован в трёх файлах (verify.py,
rewrite.py, image_pipeline.py), причём с ошибкой: при исчерпании попыток
код звал response.raise_for_status() уже после цикла, из-за чего терялось
тело ответа с реальной причиной отказа, и в логах оставалось только
"Client error 404" без объяснения.

Здесь всё в одном месте:
  * переиспользуемое соединение (httpx.Client) — не открываем новый TLS
    на каждую из сотен новостей за цикл;
  * экспоненциальный backoff с джиттером — чтобы при 429 все ретраи не
    били в провайдера синхронно;
  * различие «временная ошибка» (ретраим) и «постоянная» (сразу сдаёмся:
    404, 401, 400 ретраить бессмысленно, это только тратит время);
  * тело ответа всегда попадает в текст исключения — без этого отладка
    чужого провайдера превращается в гадание.
"""
from __future__ import annotations

import json
import random
import threading
import time
from typing import Any

import httpx

from config import settings
from logging_setup import get_logger

logger = get_logger("ai")

# Коды, при которых имеет смысл повторить запрос.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}

# Максимальная длина тела ответа в тексте ошибки — иначе HTML-страница
# ошибки от прокси целиком уедет в лог и в Telegram-сообщение.
_ERROR_BODY_LIMIT = 400


class AIProviderError(RuntimeError):
    """Ошибка обращения к AI Provider."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_permanent(self) -> bool:
        """404/401/403 не пройдут и через час — нет смысла ретраить их
        на уровне пайплайна (в отличие от 429/5xx)."""
        return self.status_code in (400, 401, 403, 404, 405, 422)


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def get_client() -> httpx.Client:
    """
    Один общий клиент на процесс. Ленивая инициализация, потокобезопасная:
    сбор новостей идёт в рабочем потоке (asyncio.to_thread), а веб-панель
    может дёрнуть тот же код из своего пула.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if settings.ai_insecure_tls:
                    logger.warning(
                        "AI_PROVIDER_INSECURE_TLS=true — сертификат провайдера "
                        "НЕ проверяется. Это временная мера на случай "
                        "протухшего сертификата; выключите её, как только "
                        "провайдер обновит сертификат."
                    )
                _client = httpx.Client(
                    timeout=httpx.Timeout(settings.ai_timeout_seconds, connect=15.0),
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                    headers={"User-Agent": "KindPoliceBot/2.0"},
                    follow_redirects=True,
                    verify=not settings.ai_insecure_tls,
                )
    return _client


def close_client() -> None:
    """Закрыть соединения при остановке бота."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _auth_headers(api_key: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key or settings.ai_api_key}",
        "Content-Type": "application/json",
    }


def _short_body(response: httpx.Response) -> str:
    try:
        text = response.text
    except Exception:
        return "<не удалось прочитать тело ответа>"
    text = " ".join(text.split())
    if len(text) > _ERROR_BODY_LIMIT:
        text = text[:_ERROR_BODY_LIMIT] + "…"
    return text


def post_json(
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
    label: str = "request",
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """
    POST на {base_url}{path} с ретраями. Возвращает разобранный JSON.
    Бросает AIProviderError с понятным текстом при любой неудаче.

    base_url и api_key позволяют обратиться к ДРУГОМУ сервису тем же
    клиентом: картинки обычно рисует не тот провайдер, что отвечает за
    текст (у текстовых прокси эндпоинта /images/generations попросту
    нет). По умолчанию берутся основные настройки.
    """
    key = api_key or settings.ai_api_key
    if not key:
        raise AIProviderError("API-ключ не задан")

    url = f"{(base_url or settings.ai_base_url).rstrip('/')}{path}"
    attempts = max_retries if max_retries is not None else settings.ai_max_retries
    attempts = max(1, attempts)
    client = get_client()

    last_error: str = "неизвестная ошибка"
    last_status: int | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = client.post(
                url,
                headers=_auth_headers(key),
                json=payload,
                timeout=timeout or settings.ai_timeout_seconds,
            )
        except httpx.RequestError as exc:
            last_error = f"сеть недоступна: {type(exc).__name__}: {exc}"
            last_status = None
            logger.warning("[%s] попытка %d/%d — %s", label, attempt, attempts, last_error)
            _sleep_backoff(attempt, attempts)
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except json.JSONDecodeError:
                raise AIProviderError(
                    f"[{label}] провайдер вернул не-JSON: {_short_body(response)}",
                    status_code=200,
                ) from None

        last_status = response.status_code
        last_error = f"HTTP {response.status_code}: {_short_body(response)}"

        if response.status_code not in RETRYABLE_STATUS:
            # Постоянная ошибка — ретраить бессмысленно, сдаёмся сразу.
            raise AIProviderError(f"[{label}] {last_error}", status_code=last_status)

        logger.warning("[%s] попытка %d/%d — %s", label, attempt, attempts, last_error)
        _sleep_backoff(attempt, attempts)

    raise AIProviderError(
        f"[{label}] исчерпаны {attempts} попыток. Последняя ошибка — {last_error}",
        status_code=last_status,
    )


def _sleep_backoff(attempt: int, attempts: int) -> None:
    """Экспоненциальная пауза с джиттером; после последней попытки не ждём."""
    if attempt >= attempts:
        return
    delay = min(2 ** attempt, 30) + random.uniform(0, 1.5)
    time.sleep(delay)


# Какие параметры запроса провайдер уже отверг. Линейка gpt-5 требует
# max_completion_tokens вместо max_tokens и не принимает temperature;
# провайдеры постарше, наоборот, знают только max_tokens. Определять
# это по имени модели бессмысленно — список моделей меняется, а у
# прокси-сервисов имена свои. Поэтому пробуем, запоминаем отказ и
# дальше в этом запуске сразу отправляем то, что работает.
_rejected_params: set[str] = set()


def _apply_known_rejections(payload: dict[str, Any]) -> dict[str, Any]:
    if "max_tokens" in _rejected_params and "max_tokens" in payload:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
    if "temperature" in _rejected_params:
        payload.pop("temperature", None)
    return payload


def _unsupported_param(message: str) -> str | None:
    """Какой параметр провайдер назвал неподдерживаемым."""
    lowered = message.lower()
    if "max_tokens" in lowered and "max_completion_tokens" in lowered:
        return "max_tokens"
    if "temperature" in lowered and (
        "unsupported" in lowered or "not supported" in lowered
    ):
        return "temperature"
    return None


def _chat_request(payload: dict[str, Any], *, label: str) -> dict:
    """
    Запрос к /chat/completions с подстройкой под требования провайдера.

    Делает не больше двух дополнительных попыток: по одной на каждый
    параметр, который провайдер может отвергнуть.
    """
    payload = _apply_known_rejections(dict(payload))

    for _ in range(3):
        try:
            return post_json("/chat/completions", payload, label=label)
        except AIProviderError as exc:
            if exc.status_code != 400:
                raise
            param = _unsupported_param(str(exc))
            if param is None or param in _rejected_params:
                raise
            _rejected_params.add(param)
            logger.info(
                "Провайдер не принимает %s — повторяю запрос без него.", param
            )
            payload = _apply_known_rejections(payload)

    raise AIProviderError(f"[{label}] провайдер отверг параметры запроса")


# Запас токенов для рассуждающих моделей: обычного лимита им не
# хватает, потому что рассуждения тратятся из того же бюджета.
_REASONING_BUDGET = 2000


def _finish_reason(data: dict) -> str | None:
    try:
        return data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


def _content_from(data: dict, *, label: str) -> str:
    """Текст ответа модели; пустая строка, если текста нет."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError(
            f"[{label}] неожиданный формат ответа: {str(data)[:_ERROR_BODY_LIMIT]}"
        ) from exc

    # Некоторые провайдеры отдают content списком блоков
    # ([{"type": "text", "text": "..."}]) вместо строки.
    if isinstance(content, list):
        content = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
        )

    return (content or "").strip()


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = 600,
    temperature: float | None = None,
    label: str = "chat",
) -> str:
    """
    Обёртка над /chat/completions — возвращает текст ответа модели.

    Пустой ответ считается ошибкой: молча вернуть "" опаснее, чем упасть,
    потому что вызывающий код тогда опубликует пустой пост.
    """
    payload: dict[str, Any] = {
        "model": model or settings.ai_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    data = _chat_request(payload, label=label)
    content = _content_from(data, label=label)
    if content:
        return content

    # Пустой ответ при finish_reason = "length" — это рассуждающая
    # модель (линейка gpt-5): лимит токенов целиком ушёл на внутренние
    # рассуждения, и до текста дело не дошло. Проверено на gpt-5-mini:
    # при лимите 300 все 300 оказались reasoning_tokens, а content
    # пришёл пустой. Повторяем один раз с запасом.
    if _finish_reason(data) == "length":
        bigger = max(_REASONING_BUDGET, max_tokens * 4)
        logger.info(
            "Модель израсходовала лимит на рассуждения — повторяю с %d токенов.",
            bigger,
        )
        payload["max_completion_tokens" if "max_completion_tokens" in payload
                else "max_tokens"] = bigger
        data = _chat_request(payload, label=label)
        content = _content_from(data, label=label)
        if content:
            return content

    raise AIProviderError(f"[{label}] провайдер вернул пустой текст")


def extract_json_object(raw: str) -> dict:
    """
    Достаёт JSON-объект из ответа модели, даже если она обернула его в
    ```json ... ``` или добавила пояснение до/после.

    Ищем от первой '{' до последней '}' — этого достаточно, потому что мы
    просим модель вернуть ровно один объект, а вложенные скобки внутри
    такого среза остаются сбалансированными.
    """
    text = raw.strip()

    if text.startswith("```"):
        # Срезаем ограждение markdown-блока вместе с ярлыком языка.
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"в ответе модели нет JSON-объекта: {raw[:200]}")

    return json.loads(text[start:end + 1])
