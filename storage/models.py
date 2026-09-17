"""
Единая модель "новости" — то, что передаётся между этапами пайплайна:
источник -> верификация -> рерайт -> изображение -> модерация -> публикация.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class NewsStatus(str, Enum):
    NEW = "new"                    # только что собрано из источника
    VERIFIED = "verified"          # прошло проверку подлинности
    REJECTED = "rejected"          # отклонено (фейк / негатив / дубликат)
    REWRITTEN = "rewritten"        # текст переписан
    IMAGE_READY = "image_ready"    # подобрано/сгенерировано изображение
    PENDING_MODERATION = "pending_moderation"
    APPROVED = "approved"          # одобрено модератором, готово к публикации
    PUBLISHED = "published"


@dataclass
class RawNewsItem:
    """То, что отдаёт адаптер источника — сырой, неочищенный материал."""
    source_name: str
    source_url: str
    title: str
    raw_text: str
    published_at: datetime
    image_url: str | None = None


@dataclass
class NewsItem:
    """Новость на любом этапе обработки."""
    id: str
    source_name: str
    source_url: str
    title: str
    raw_text: str
    published_at: datetime
    status: NewsStatus = NewsStatus.NEW

    region: str | None = None
    city: str | None = None

    rewritten_text: str | None = None
    image_path: str | None = None
    image_source: str | None = None  # "original" | "generated"

    verification_notes: str = ""
    rejection_reason: str | None = None
    # Машиночитаемая причина отказа (negative_marker / routine / ...) —
    # по ней строится статистика /stats, в отличие от rejection_reason,
    # который пишется свободным текстом для человека.
    reject_code: str | None = None

    # Кто и когда обработал новость — нужно, чтобы в чате модераторов
    # было видно авторство решения, а не безличное «опубликовано».
    moderated_by: int | None = None
    # message_id опубликованного поста: позволяет позже отредактировать
    # или удалить пост, если модератор передумал.
    published_message_id: int | None = None

    tags: list[str] = field(default_factory=list)


class UserRole(str, Enum):
    """
    ADMIN — может назначать/снимать модераторов, имеет доступ ко всему,
      что доступно модератору.
    MODERATOR — доступ к /fetch, /pending, /clear, кнопкам
      "Одобрить"/"Отклонить".
    Пользователи без записи в bot_users не имеют доступа ни к чему.
    """
    ADMIN = "admin"
    MODERATOR = "moderator"


@dataclass
class BotUser:
    """
    Человек с доступом к управляющим командам бота (не читатель канала).
    """
    telegram_id: int
    role: UserRole
    username: str | None = None
    full_name: str | None = None
    added_by: int | None = None
    added_at: datetime = field(default_factory=datetime.utcnow)


class ResourceCategory(str, Enum):
    """Категории официальных источников для публичного меню бота."""
    MVD_TELEGRAM = "mvd_telegram"
    MVD_MAX = "mvd_max"
    INSTITUTE = "institute"  # учебные заведения МВД по регионам


@dataclass
class PublicResource:
    """
    Ссылка на официальный источник (канал МВД, страница учебного
    заведения и т.п.), которую бот показывает обычным пользователям
    через /resources.
    """
    id: str
    category: str
    name: str
    url: str
    region: str | None = None
    added_by: int | None = None
    added_at: datetime = field(default_factory=datetime.utcnow)
