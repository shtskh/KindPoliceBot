"""
Центральная конфигурация бота.

Все секреты берутся из переменных окружения или из файла .env в корне
проекта — никогда не хардкодятся в коде. Шаблон см. в .env.example.

ВАЖНО про .env: файл содержит живые токены, его нельзя коммитить в git
и нельзя хранить в .idea/workspace.xml (IDE-конфиг часто попадает в
репозиторий вместе с проектом). См. .gitignore.
"""
import os
from dataclasses import dataclass, field

from paths import DEFAULT_DB_PATH, PROJECT_ROOT

# --- Загрузка .env ---------------------------------------------------------
# python-dotenv не обязателен: если он не установлен, просто работаем на
# «голых» переменных окружения, как раньше. Это позволяет не ломать
# существующие запуски через IDE/systemd, где env задаётся снаружи.
try:  # pragma: no cover - тривиальный импорт
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _parse_id_list(raw: str) -> list[int]:
    """BOT_ADMIN_IDS="123456789,987654321" -> [123456789, 987654321]"""
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            pass
    return ids


def _parse_str_list(raw: str, default: list[str]) -> list[str]:
    """Список через запятую из env; пустое значение -> default."""
    items = [p.strip() for p in raw.split(",") if p.strip()]
    return items or list(default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


# Куда ведут ссылки на канал и бота в этой версии.
PUBLIC_LINK_BASE = "https://t.me/"


@dataclass
class Settings:
    # --- Токены доставки ---
    # ВАЖНО: секреты берутся ТОЛЬКО из переменных окружения / .env,
    # без дефолтов. Если токен когда-либо был захардкожен в коде или
    # попал в .idea/workspace.xml — его нужно отозвать и перевыпустить.
    max_bot_token: str = os.getenv("MAX_BOT_TOKEN", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # Чат модераторов (курсанты/кураторы) — сюда бот присылает посты
    # на одобрение с кнопками Одобрить/Отклонить.
    moderator_chat_id: str = os.getenv("MODERATOR_CHAT_ID", "")
    # Канал/чат, куда публикуются одобренные посты.
    publish_chat_id: str = os.getenv("PUBLISH_CHAT_ID", "")
    # Куда дублировать посты в MAX (когда появится боевой токен).
    max_publish_chat_id: str = os.getenv("MAX_PUBLISH_CHAT_ID", "")

    # Какие каналы доставки активны. На старте разработки удобно
    # держать только Telegram (не требует верификации юрлица),
    # когда будет готов токен Max — добавить "max" в DELIVERY_CHANNELS.
    active_delivery_channels: list[str] = field(
        default_factory=lambda: _parse_str_list(
            os.getenv("DELIVERY_CHANNELS", ""), ["telegram"]
        )
    )

    # --- AI Provider (OpenAI-совместимый эндпоинт) ---
    # Один провайдер обслуживает и верификацию, и рерайт, и (если у него
    # есть такой эндпоинт) генерацию изображений.
    ai_api_key: str = os.getenv("AI_PROVIDER_API_KEY", "")
    ai_base_url: str = os.getenv(
        "AI_PROVIDER_BASE_URL", "https://aiprovider.duckdns.org/v1"
    ).rstrip("/")
    ai_model: str = os.getenv("AI_PROVIDER_MODEL", "claude-sonnet-4-6").strip()
    # Отдельная (более дешёвая/быстрая) модель для массовой верификации:
    # её вызывают на каждой новости, а рерайт — только на прошедших отбор.
    ai_verify_model: str = os.getenv("AI_PROVIDER_VERIFY_MODEL", "").strip()
    ai_timeout_seconds: float = float(os.getenv("AI_TIMEOUT_SECONDS", "90"))
    # АВАРИЙНЫЙ режим: не проверять TLS-сертификат провайдера.
    #
    # Нужен ровно в одном случае — у провайдера протух сертификат, и все
    # запросы падают с «certificate has expired», хотя сервис работает.
    # Так было 16.10.2026: Let's Encrypt не продлился, и бот остался без
    # отбора новостей и без помощника.
    #
    # ЦЕНА: без проверки сертификата нельзя отличить настоящий сервер от
    # подменённого, то есть API-ключ уходит в канал, который теоретически
    # можно перехватить. Включать только как временную меру и выключать,
    # как только сертификат продлят.
    ai_insecure_tls: bool = _env_bool("AI_PROVIDER_INSECURE_TLS", False)
    ai_max_retries: int = _env_int("AI_MAX_RETRIES", 3)

    # --- Генерация изображений ---
    #
    # Сервис картинок задаётся ОТДЕЛЬНО от текстового: у текстовых
    # прокси (и у baza-ai, и у прежнего duckdns) эндпоинта
    # /images/generations нет вовсе — проверено, отвечает 404 на любой
    # модели. Поэтому ключ и адрес для картинок свои.
    #
    # Подходит любой сервис с OpenAI-совместимым /images/generations.
    # Если IMAGE_API_KEY не задан, берётся основной ключ — это рабочий
    # вариант, когда один провайдер умеет и текст, и картинки.
    image_generation_enabled: bool = _env_bool("IMAGE_GENERATION_ENABLED", True)
    # По умолчанию ProxyAPI: проверено вживую, оплата в рублях, работает
    # с сервера в РФ без VPN. Прямой OpenAI — https://api.openai.com/v1
    image_api_base: str = os.getenv(
        "IMAGE_API_BASE", "https://api.proxyapi.ru/openai/v1"
    ).rstrip("/")
    image_api_key_raw: str = os.getenv("IMAGE_API_KEY", "")
    ai_image_endpoint: str = os.getenv(
        "AI_PROVIDER_IMAGE_ENDPOINT", "/images/generations"
    )
    # Несколько кандидатов подряд: провайдеры называют модели по-разному,
    # а модели OpenAI быстро сменяют друг друга (gpt-image-1 закрыта
    # 23.10.2026). Перебор избавляет от правки кода при каждой смене.
    ai_image_models: list[str] = field(
        default_factory=lambda: _parse_str_list(
            os.getenv("AI_PROVIDER_IMAGE_MODELS", ""),
            [
                "gpt-image-2.5-flare",
                "gpt-image-2.5-sunburst",
                "gpt-image-2.6-flare",
                "dall-e-3",
                "flux.1-schnell",
            ],
        )
    )
    ai_image_size: str = os.getenv("AI_PROVIDER_IMAGE_SIZE", "1024x1024")
    # low | medium | high — выше качество, дороже и дольше.
    ai_image_quality: str = os.getenv("AI_PROVIDER_IMAGE_QUALITY", "medium")
    # Надпись на сгенерированной картинке. Пустая строка отключает её,
    # но для ведомственной темы это плохая идея: без пометки рисунок
    # выглядит как фотография с места события.
    # Рисовать ли шевроны и кокарды крупно. По умолчанию нет:
    # генератор подделывает надпись «МВД» на шевроне, и при
    # увеличении это видно. Форма при этом всё равно узнаваемая.
    ai_image_show_insignia: bool = _env_bool("AI_IMAGE_SHOW_INSIGNIA", False)
    ai_image_label: str = os.getenv(
        "AI_IMAGE_LABEL", "Иллюстрация · сгенерировано ИИ"
    )
    # Рисовать фирменную карточку, если фото нет и генерация не удалась.
    # Это гарантирует, что у КАЖДОГО поста будет изображение.
    image_card_fallback: bool = _env_bool("IMAGE_CARD_FALLBACK", True)

    # --- ИИ-помощник для обычных пользователей ---
    # Отвечает на бытовые правовые вопросы (куда обращаться, какие права,
    # что делать по шагам). Ограничения и дисклеймеры — в
    # pipeline/assistant.py, отключить их настройкой нельзя.
    assistant_enabled: bool = _env_bool("ASSISTANT_ENABLED", True)
    # Отдельная модель: помощник отвечает людям напрямую, здесь имеет
    # смысл модель поумнее, чем для массовой фильтрации новостей.
    assistant_model_name: str = os.getenv("ASSISTANT_MODEL", "").strip()
    # Сколько вопросов один человек может задать за сутки. Без лимита
    # один пользователь способен исчерпать бюджет провайдера за вечер.
    # 0 — без ограничений (не рекомендуется в проде).
    assistant_daily_limit: int = _env_int("ASSISTANT_DAILY_LIMIT", 15)

    # --- Источники: официальные ленты МВД ---
    #
    # ГЛАВНЫЙ ИСТОЧНИК. У МВД есть RSS и федеральный, и у каждого
    # территориального управления: NN.мвд.рф/news/rss, где NN — код
    # региона. Проверено: отвечает 81 региональная лента по 20 новостей,
    # то есть больше 1600 первоисточников за один обход.
    #
    # Раньше бот жил на двух общих лентах СМИ и приносил единицы
    # подходящих новостей: полиция попадает в федеральные ленты редко,
    # а в региональные ленты МВД — постоянно. Здесь же сразу известен
    # регион, что важно для карточки и для раздела «Мой регион».
    mvd_rss_enabled: bool = _env_bool("MVD_RSS_ENABLED", True)

    # Коды регионов, чьи ленты реально отвечают (проверено запросом).
    # Отсутствующие здесь коды либо не имеют RSS, либо недоступны.
    mvd_rss_regions: list[str] = field(
        default_factory=lambda: _parse_str_list(
            os.getenv("MVD_RSS_REGIONS", ""),
            [
                "01", "02", "03", "04", "05", "06", "07", "08", "09", "10",
                "11", "12", "13", "14", "15", "16", "17", "18", "19", "21",
                "22", "23", "24", "25", "26", "27", "28", "29", "30", "31",
                "32", "33", "36", "37", "38", "39", "40", "41", "42", "43",
                "44", "45", "46", "48", "49", "50", "51", "52", "53", "54",
                "55", "56", "57", "58", "59", "60", "61", "62", "63", "64",
                "65", "66", "67", "68", "69", "70", "71", "72", "73", "74",
                "75", "76", "77", "78", "79", "82", "83", "86", "87", "89",
                "92",
            ],
        )
    )

    # --- Источники: общие ленты СМИ ---
    # Шумные: полиция там — малая доля, поэтому к ним применяется
    # keyword-фильтр (police_keywords) ещё до обращения к модели.
    rss_sources: list[str] = field(
        default_factory=lambda: _parse_str_list(
            os.getenv("RSS_SOURCES", ""),
            [
                "https://мвд.рф/news/rss",                        # МВД России
                "https://ria.ru/export/rss2/archive/index.xml",   # РИА Новости
                "https://tass.ru/rss/v2.xml",                     # ТАСС
                "https://rg.ru/xml/index.xml",                    # Российская газета
                "https://www.interfax.ru/rss.asp",                # Интерфакс
                "https://lenta.ru/rss",                           # Лента.ру
                "https://iz.ru/xml/rss/all.xml",                  # Известия
                "https://www.kommersant.ru/RSS/news.xml",         # Коммерсантъ
                "https://regnum.ru/rss",                          # Регнум
                "https://www.mk.ru/rss/news/index.xml",           # МК
                "https://360.ru/rss/",                            # 360
            ],
        )
    )

    # --- Источники: официальные Telegram-каналы (без токена, через t.me/s/) ---
    # @rumvd помечен как НЕОФИЦИАЛЬНЫЙ — он собирается, но не считается
    # доверенным источником (см. trusted_telegram_channels ниже).
    telegram_channels: list[str] = field(default_factory=lambda: [
        "mediamvd",         # МВД России — официальный
        "rumvd",            # МВД РФ — НЕОФИЦИАЛЬНЫЙ канал, ручная модерация
        "genprocrf",        # Генпрокуратура РФ — официальный
        "sledcom_press",    # Следственный комитет РФ — официальный
        "cyberpolice_rus",  # Вестник Киберполиции России
        "IrinaVolk_MVD",    # Ирина Волк, офиц. представитель МВД
        "migrpost",         # Миграционный пост
        # Раньше здесь стоял "gibdd_ru" — такого канала не существует,
        # t.me/s/gibdd_ru отдаёт пустую страницу, и источник молча
        # ничего не приносил на каждом сборе. Действующий канал — @gibdd.
        "gibdd",            # Госавтоинспекция МВД России
        "rosgvardia_ru",    # Росгвардия
        "mospolice",        # Полиция Москвы
    ])

    trusted_telegram_channels: list[str] = field(default_factory=lambda: [
        "mediamvd", "genprocrf", "sledcom_press", "cyberpolice_rus",
        "IrinaVolk_MVD", "migrpost", "gibdd", "rosgvardia_ru", "mospolice",
        # "rumvd" сознательно НЕ включён — неофициальный канал
    ])

    # Ключевые слова для предварительного отбора новостей о полиции
    # из общих РИА/ТАСС лент.
    police_keywords: list[str] = field(default_factory=lambda: [
        "полиц", "мвд", "участков", "гибдд", "госавтоинспекц",
        "следовател", "оперативник", "патрульно-постовой", "росгвард",
        "правоохранител", "стражи порядка", "дпс", "ппс",
    ])

    # --- Хранилище ---
    db_path: str = os.getenv("DB_PATH", str(DEFAULT_DB_PATH))

    # --- Сбор ---
    # Сколько постов брать с каждого источника за один цикл.
    fetch_per_source: int = _env_int("FETCH_PER_SOURCE", 20)
    # Сколько ГОТОВЫХ постов набрать за цикл, прежде чем остановиться.
    fetch_limit: int = _env_int("FETCH_LIMIT", 10)
    # Потолок обращений к модели за цикл. Источников теперь больше
    # полутора тысяч новостей за обход, и проверять их все моделью —
    # это и долго, и дорого. Кандидаты сортируются эвристикой
    # (pipeline/ranking.py), модель смотрит только лучших.
    max_llm_checks: int = _env_int("MAX_LLM_CHECKS", 80)
    # Не тратить LLM на новости старше N дней — это уже не новости.
    max_news_age_days: int = _env_int("MAX_NEWS_AGE_DAYS", 7)

    # --- Доступ к боту (роли) ---
    # Telegram user_id людей, которые при первом запуске бота автоматически
    # получат роль admin. Узнать свой user_id можно у @userinfobot.
    bot_admin_ids: list[int] = field(
        default_factory=lambda: _parse_id_list(os.getenv("BOT_ADMIN_IDS", ""))
    )

    # --- Доступ к веб-панели модерации (HTTP Basic Auth) ---
    # ВАЖНО: Basic Auth передаёт логин/пароль в base64 (не шифрует их) —
    # панель обязательно должна быть за HTTPS в проде (nginx/Caddy как
    # reverse proxy), иначе пароль виден любому, кто перехватит трафик.
    panel_username: str = os.getenv("PANEL_USERNAME", "")
    panel_password: str = os.getenv("PANEL_PASSWORD", "")

    # --- Витрина для экрана ---
    #
    # Страница /screen крутит опубликованные посты на экране в холле,
    # в учебном классе или на стенде. Она ПУБЛИЧНАЯ, без пароля: на
    # экране показывается только то, что уже вышло в канал, то есть
    # никакой закрытой информации там нет, а требовать логин на
    # устройстве без клавиатуры неудобно. Очередь модерации по-прежнему
    # закрыта паролем.
    screen_enabled: bool = _env_bool("SCREEN_ENABLED", True)
    # Сколько секунд висит один пост. Полторы минуты — компромисс:
    # хватает прочитать текст, и экран не выглядит застывшим.
    screen_interval_seconds: int = _env_int("SCREEN_INTERVAL_SECONDS", 90)
    # Сколько последних постов крутится по кругу.
    screen_items: int = _env_int("SCREEN_ITEMS", 20)
    # Как часто страница забирает с сервера свежий список. Экран
    # работает сутками, перезагружать его вручную никто не будет.
    screen_refresh_seconds: int = _env_int("SCREEN_REFRESH_SECONDS", 300)
    # QR-код на канал в углу экрана: зритель наводит телефон и
    # подписывается, не записывая адрес с экрана.
    screen_qr_enabled: bool = _env_bool("SCREEN_QR_ENABLED", True)
    # Куда ведёт QR. Пусто — ссылка на канал из CHANNEL_USERNAME.
    screen_link_raw: str = os.getenv("SCREEN_LINK", "")

    # --- Веб-сервер ---
    #
    # Панель модерации и витрина живут в app.py. Хостинг запускает
    # только main.py, поэтому сервер поднимается прямо оттуда в
    # отдельном потоке: иначе /screen на сервере просто некому отдать.
    web_enabled: bool = _env_bool("WEB_ENABLED", True)
    # Порт: хостинги обычно передают его в PORT, поэтому читаем сначала
    # его, а WEB_PORT оставляем для ручной настройки.
    web_port: int = _env_int("WEB_PORT", _env_int("PORT", 8000))
    web_host: str = os.getenv("WEB_HOST", "0.0.0.0")

    # --- Обязательная подпись в конце каждого опубликованного поста ---
    # Юзернеймы БЕЗ @. Это публичная информация (не секрет).
    channel_username: str = os.getenv("CHANNEL_USERNAME", "kindpolice")
    bot_username: str = os.getenv("BOT_USERNAME", "kindmvdbot")
    channel_title: str = os.getenv("CHANNEL_TITLE", "Добрые новости о полиции")

    # --- Настройки публикации по умолчанию ---
    default_post_interval_minutes: int = _env_int("POST_INTERVAL_MINUTES", 120)
    require_moderation_before_publish: bool = True  # на старте — всегда True

    # --- Логирование ---
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_to_file: bool = _env_bool("LOG_TO_FILE", True)

    @property
    def screen_link(self) -> str:
        """Адрес канала для QR-кода на витрине."""
        if self.screen_link_raw:
            return self.screen_link_raw
        if self.channel_username:
            return f"{PUBLIC_LINK_BASE}{self.channel_username}"
        return ""

    @property
    def image_api_key(self) -> str:
        """Ключ сервиса картинок; если свой не задан — основной."""
        return self.image_api_key_raw or self.ai_api_key

    @property
    def image_generation_configured(self) -> bool:
        return bool(self.image_generation_enabled and self.image_api_key)

    @property
    def verify_model(self) -> str:
        """Модель верификации: отдельная, если задана, иначе основная."""
        return self.ai_verify_model or self.ai_model

    @property
    def assistant_model(self) -> str:
        """Модель помощника: отдельная, если задана, иначе основная."""
        return self.assistant_model_name or self.ai_model

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_api_key)


settings = Settings()


def validate_settings() -> list[str]:
    """
    Вызывать при старте бота/панели. Не бросает исключение сама по себе
    (чтобы, например, /pending можно было посмотреть даже без части
    токенов), но громко предупреждает в логах о том, чего не хватает —
    вместо тихой работы с пустыми/чужими значениями.

    Возвращает список отсутствующих настроек — вызывающий код может
    показать его администратору (см. /diag в боте).
    """
    import logging

    missing: list[str] = []
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN — бот не сможет запуститься")
    if not settings.moderator_chat_id:
        missing.append("MODERATOR_CHAT_ID — некуда слать посты на модерацию")
    if not settings.publish_chat_id:
        missing.append("PUBLISH_CHAT_ID — некуда публиковать одобренное")
    if not settings.ai_api_key:
        missing.append(
            "AI_PROVIDER_API_KEY — без него не работают отбор и рерайт, "
            "все новости пойдут на ручную модерацию в сыром виде"
        )
    if not settings.bot_admin_ids:
        missing.append(
            "BOT_ADMIN_IDS — без него некому назначать модераторов через бота"
        )
    if not settings.panel_username or not settings.panel_password:
        missing.append(
            "PANEL_USERNAME/PANEL_PASSWORD — веб-панель модерации откажет в доступе"
        )

    if missing:
        logging.getLogger("police-news-bot").warning(
            "Не заданы переменные окружения:\n  - %s", "\n  - ".join(missing)
        )
    return missing
