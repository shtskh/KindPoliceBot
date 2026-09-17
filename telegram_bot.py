"""
Telegram-бот проекта «Хороший полицейский».

Две роли в одном боте:
  * ПУБЛИЧНАЯ — витрина официальных источников (/resources): каналы МВД
    в Telegram и MAX, образовательные организации МВД по регионам.
    Доступна всем без исключения.
  * СЛУЖЕБНАЯ — сбор, модерация и публикация новостей. Доступна только
    пользователям с ролью moderator/admin.

Запуск:
    скопировать .env.example в .env, заполнить значения, затем
    python telegram_bot.py

Как узнать chat_id: добавить бота в чат/канал, отправить любое сообщение
и посмотреть /chatid — бот ответит идентификатором текущего чата.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import sys
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.text_decorations import html_decoration
from aiogram.utils.token import TokenValidationError

from config import settings, validate_settings
from data.help_content import (
    ABOUT_TEXT, DISCLAIMER, EDUCATION_INFO, EMERGENCY_PHONES,
    SITUATION_BY_KEY, SITUATIONS, TRUST_PHONES,
)
from data.resources_seed import build_seed_resources, seed_counts
from pipeline import assistant
from delivery.telegram_delivery import TelegramDelivery, strip_html
from logging_setup import setup_logging
from paths import resolve
from pipeline import image_pipeline
from pipeline.orchestrator import (
    CollectionStats, regenerate_image, run_collection_cycle,
)
from pipeline.post_formatting import FOOTER_MARKER
from storage import db
from storage.models import (
    BotUser, NewsStatus, PublicResource, ResourceCategory, UserRole,
)

logger = setup_logging(settings.log_level, settings.log_to_file)

# ВАЖНО: parse_mode задаётся здесь, в DefaultBotProperties.
#
# БЫЛ БАГ: в старой версии на уровне модуля стояла строка
#     parse_mode = ParseMode.HTML
# — обычная переменная, которая никуда не передавалась. Bot() создавался
# без parse_mode, поэтому все вызовы, где его не указали явно (в
# частности edit_text/edit_caption после нажатия «Одобрить»), отправляли
# HTML как ПЛОСКИЙ ТЕКСТ: модератор видел в чате буквальное
# «<b>ОПУБЛИКОВАНО</b>» и сырые теги вместо разметки.
#
# Bot создаётся на уровне модуля, потому что декораторы @dp.* и хендлеры
# ссылаются на него. Но aiogram проверяет формат токена прямо в
# конструкторе и бросает TokenValidationError — при пустом или кривом
# токене падал сам ИМПОРТ модуля, с трассировкой вместо объяснения, и
# validate_settings() со своим понятным предупреждением просто не успевал
# отработать. Поэтому ошибку ловим здесь и откладываем до main().
def _create_bot() -> Bot | None:
    if not settings.telegram_bot_token:
        return None
    try:
        return Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
    except TokenValidationError:
        logger.error(
            "TELEGRAM_BOT_TOKEN имеет неверный формат. Ожидается вид "
            "123456789:AAE... — проверьте значение в .env."
        )
        return None


bot = _create_bot()

dp = Dispatcher()

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


# ==========================================================================
# Доступ (роли)
# ==========================================================================

_ROLE_ORDER = {UserRole.MODERATOR: 1, UserRole.ADMIN: 2}


def _role_allows(role: UserRole, minimum: UserRole) -> bool:
    return _ROLE_ORDER[role] >= _ROLE_ORDER[minimum]


async def _require_role(message: Message, minimum: UserRole) -> bool:
    # В каналах и анонимных админских сообщениях from_user отсутствует —
    # без этой проверки хендлер падал с AttributeError.
    if message.from_user is None:
        return False

    user = db.get_user(settings.db_path, message.from_user.id)
    if user is None or not _role_allows(user.role, minimum):
        await message.answer(
            "⛔ У вас нет доступа к этой команде.\n\n"
            "Если вы курсант или куратор проекта — попросите администратора "
            "добавить вас через /add_moderator."
        )
        logger.warning(
            "Отказано в доступе: user_id=%s username=%s command=%s",
            message.from_user.id, message.from_user.username, message.text,
        )
        return False
    return True


async def _require_role_callback(callback: CallbackQuery, minimum: UserRole) -> bool:
    user = db.get_user(settings.db_path, callback.from_user.id)
    if user is None or not _role_allows(user.role, minimum):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        logger.warning(
            "Отказано в доступе (callback): user_id=%s username=%s",
            callback.from_user.id, callback.from_user.username,
        )
        return False
    return True


def bootstrap_admins() -> None:
    """
    Гарантирует, что все telegram_id из BOT_ADMIN_IDS имеют роль admin,
    иначе после первого деплоя некому будет назначать модераторов.

    Если человек ранее был модератором, роль повышается до admin —
    раньше существующая запись не трогалась вовсе, и добавление id в
    BOT_ADMIN_IDS не давало никакого эффекта.
    """
    for admin_id in settings.bot_admin_ids:
        existing = db.get_user(settings.db_path, admin_id)
        if existing is None or existing.role != UserRole.ADMIN:
            db.upsert_user(
                settings.db_path,
                BotUser(
                    telegram_id=admin_id,
                    role=UserRole.ADMIN,
                    username=existing.username if existing else None,
                    full_name=existing.full_name if existing else None,
                ),
            )
            logger.info("Администратор из BOT_ADMIN_IDS: %s", admin_id)


def seed_public_resources(update_existing: bool = False) -> int:
    """
    Заполняет публичное меню официальными источниками.

    Идемпотентно и одной транзакцией: раньше каждая запись вставлялась
    отдельным соединением, и на 124 записях старт бота заметно тормозил.
    """
    added = db.add_resources_bulk(
        settings.db_path, build_seed_resources(), update_existing=update_existing
    )
    if added:
        logger.info("В публичное меню добавлено источников: %d", added)
    return added


# ==========================================================================
# Оформление карточки модерации
# ==========================================================================

_MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

_IMAGE_SOURCE_LABELS = {
    "original": "фото из источника",
    "generated": "сгенерировано ИИ",
    "card": "фирменная карточка",
    "none": "без изображения",
}


def _format_date_ru(value: datetime | None) -> str:
    if not value:
        return ""
    try:
        return f"{value.day} {_MONTHS_RU[value.month - 1]} {value.year}"
    except (IndexError, ValueError, AttributeError):
        return ""


def clean_moderation_text(text: str) -> str:
    """
    Убирает из текста строки-мусор, которые иногда переживают рерайт:
    призывы подписаться, служебные пометки, голые ссылки.

    Слова-маркеры проверяются как ОТДЕЛЬНЫЕ слова, а не как подстроки.
    Раньше в списке были «max» и «vk», и проверка `fragment in lower`
    выбрасывала любую строку, где эти буквы встречались внутри слова.
    """
    if not text:
        return ""

    bad_substrings = (
        "подписывайтесь", "подписаться", "изображение: original",
        "наш телеграм", "наш telegram",
    )
    bad_words = {"telegram:", "max:", "vk:", "vk.com", "t.me"}

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        lower = line.lower()
        if any(fragment in lower for fragment in bad_substrings):
            continue
        if bad_words & set(lower.replace(",", " ").split()):
            continue
        if lower.startswith(("http://", "https://", "www.")):
            continue

        cleaned_lines.append(line)

    cleaned = " ".join(cleaned_lines)
    return " ".join(cleaned.split())


def build_post_text(item) -> str:
    """Текст, который уходит В КАНАЛ (без служебной информации)."""
    if not item.rewritten_text:
        return html.escape(clean_moderation_text(item.raw_text or item.title or ""))

    body_part, marker, footer_part = item.rewritten_text.partition(FOOTER_MARKER)
    body = clean_moderation_text(body_part)

    parts = [body] if body else []
    if footer_part:
        parts.append(marker + footer_part)
    return "\n\n".join(parts).strip()


def build_moderation_text(item, *, compact: bool = False) -> str:
    """
    Карточка для чата модераторов: сам пост плюс служебная справка —
    место, дата, источник, происхождение картинки и вердикт фильтра.
    Модератору важно видеть не только текст, но и на чём основано
    решение бота.

    compact=True — версия для подписи к фото (лимит 1024 символа).
    """
    lines: list[str] = []

    place_parts = [p for p in (item.city, item.region) if p]
    header_bits: list[str] = []
    if place_parts:
        header_bits.append(f"📍 <b>{html.escape(' · '.join(place_parts))}</b>")
    date_label = _format_date_ru(item.published_at)
    if date_label:
        header_bits.append(f"🗓 {date_label}")
    if header_bits:
        lines.append("   ".join(header_bits))

    post_text = build_post_text(item)
    if post_text:
        lines.append(post_text)
    else:
        lines.append("<i>Текст новости пуст — публиковать нечего.</i>")

    # --- служебный блок ---
    footer: list[str] = ["➖➖➖➖➖"]

    image_label = _IMAGE_SOURCE_LABELS.get(item.image_source, item.image_source or "нет")
    footer.append(f"🖼 {image_label}")
    footer.append(f"🔗 {html.escape(item.source_name or 'источник неизвестен')}")

    if not compact:
        if item.tags:
            footer.append("🏷 " + ", ".join(html.escape(t) for t in item.tags))
        if item.verification_notes:
            notes = html.escape(item.verification_notes)
            icon = "✅" if "доверенный" in item.verification_notes.lower() else "⚠️"
            footer.append(f"{icon} <i>{notes}</i>")

    lines.append("\n".join(footer))
    return "\n\n".join(lines).strip()


def _fit_html(text: str, limit: int) -> str:
    """
    Обрезает текст под лимит Telegram, не ломая HTML-разметку.

    БЫЛ БАГ: подпись к фото резалась как text[:1024]. Обрыв посреди тега
    («<b») или потеря закрывающего </b> делают разметку невалидной, и
    Telegram отклоняет ВСЁ сообщение с ошибкой 400 can't parse entities —
    то есть новость не доходила до модератора вообще.
    """
    if len(text) <= limit:
        return text

    plain = strip_html(text)
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1].rstrip() + "…"


def moderation_keyboard(item_id: str, has_image: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve:{item_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{item_id}"),
        ],
        [
            InlineKeyboardButton(
                text="🖼 Другая картинка", callback_data=f"reimg:{item_id}"
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_item_to_moderators(item, chat_id: str | int | None = None) -> None:
    """Отправляет одну новость в чат модераторов — фото с подписью, а
    если фото нет или Telegram его не принял, обычным сообщением."""
    target = chat_id or settings.moderator_chat_id
    if not target:
        logger.warning("MODERATOR_CHAT_ID не задан — некуда отправлять новость.")
        return

    keyboard = moderation_keyboard(item.id, bool(item.image_path))

    if item.image_path and image_pipeline.image_exists(item.image_path):
        caption = _fit_html(build_moderation_text(item, compact=True), CAPTION_LIMIT)
        try:
            await bot.send_photo(
                chat_id=target,
                photo=FSInputFile(resolve(item.image_path)),
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            logger.warning(
                "Telegram отклонил фото для новости %s (%s) — отправляю текстом.",
                item.id, exc.message,
            )
        except Exception:
            logger.exception("Не удалось отправить фото для новости %s", item.id)

    text = _fit_html(build_moderation_text(item), MESSAGE_LIMIT)
    if not text:
        text = "📍 <b>Без указания места</b>\n\n<i>Новость без текста.</i>"

    await bot.send_message(chat_id=target, text=text, reply_markup=keyboard)


# ==========================================================================
# Автосбор по расписанию
# ==========================================================================

SCHEDULER_CONFIG_KEY = "scheduler"
MIN_INTERVAL_MINUTES = 10  # защита от слишком частых запросов к источникам
SCHEDULER_CHECK_EVERY_SECONDS = 60

_collection_lock = asyncio.Lock()


def _get_scheduler_state() -> dict:
    return db.get_config(
        settings.db_path,
        SCHEDULER_CONFIG_KEY,
        default={
            "enabled": False,
            "interval_minutes": settings.default_post_interval_minutes,
            "last_run_at": None,
        },
    )


def _set_scheduler_state(state: dict) -> None:
    db.set_config(settings.db_path, SCHEDULER_CONFIG_KEY, state)


def _mark_scheduler_run() -> None:
    """
    Отмечает факт запуска автосбора.

    БЫЛ БАГ: время последнего запуска записывалось только после успешного
    цикла. Если сбор падал (например, провайдер вернул 500), last_run_at
    оставался старым, планировщик через минуту считал, что снова пора, и
    так по кругу — источники и AI-провайдер получали шквал запросов
    вместо одного раза в заданный интервал.
    """
    state = _get_scheduler_state()
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    _set_scheduler_state(state)


async def run_collection_and_notify(
    progress_message: Message | None = None,
) -> tuple[list, CollectionStats]:
    """
    Общая логика «собрать + разослать на модерацию» — используется и
    /fetch, и планировщиком, чтобы не запускать два сбора одновременно.
    """
    if _collection_lock.locked():
        raise RuntimeError("Сбор новостей уже выполняется, подожди его завершения.")

    async with _collection_lock:
        loop = asyncio.get_running_loop()
        last_shown = ""

        def progress(text: str) -> None:
            # Сбор идёт в отдельном потоке, а редактировать сообщение
            # можно только из основного цикла событий.
            nonlocal last_shown
            if not progress_message or text == last_shown:
                return
            last_shown = text
            asyncio.run_coroutine_threadsafe(
                _safe_edit(progress_message, f"⏳ {text}"), loop
            )

        items, stats = await asyncio.to_thread(
            run_collection_cycle, settings.fetch_limit, progress
        )

        for item in items:
            try:
                await send_item_to_moderators(item)
            except Exception:
                logger.exception("Не удалось отправить новость %s модераторам", item.id)

        return items, stats


async def _safe_edit(message: Message, text: str) -> None:
    """Редактирование, которое не падает на «message is not modified»."""
    with contextlib.suppress(TelegramBadRequest):
        await message.edit_text(text)


async def scheduler_loop() -> None:
    """
    Фоновая задача: раз в минуту проверяет, не пора ли запускать
    автосбор. Проверка раз в минуту (а не asyncio.sleep(interval)) нужна,
    чтобы /set_interval и /schedule_on применялись сразу, без
    перезапуска бота.
    """
    while True:
        await asyncio.sleep(SCHEDULER_CHECK_EVERY_SECONDS)
        try:
            state = _get_scheduler_state()
            if not state.get("enabled"):
                continue

            interval_minutes = state.get(
                "interval_minutes", settings.default_post_interval_minutes
            )
            last_run_at = state.get("last_run_at")
            due = True
            if last_run_at:
                try:
                    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_run_at)
                    due = elapsed >= timedelta(minutes=interval_minutes)
                except ValueError:
                    due = True  # испорченная метка времени — считаем, что пора

            if not due:
                continue

            if _collection_lock.locked():
                logger.info("Плановый автосбор пропущен — уже идёт другой сбор.")
                continue

            if not settings.moderator_chat_id:
                logger.warning("Автосбор пропущен — MODERATOR_CHAT_ID не задан.")
                continue

            logger.info("Запускаю плановый автосбор...")
            # Отмечаем ДО запуска: даже если цикл упадёт, следующая
            # попытка будет по расписанию, а не через минуту.
            _mark_scheduler_run()

            items, stats = await run_collection_and_notify()

            if items:
                await bot.send_message(
                    settings.moderator_chat_id,
                    f"🕒 <b>Автосбор завершён</b>\n\n" + "\n".join(stats.summary_lines()),
                )
            logger.info("Плановый автосбор завершён: %d новостей", len(items))

        except Exception:
            logger.exception("Ошибка планового автосбора")


# ==========================================================================
# Публичное меню — официальные источники
# ==========================================================================

# Короткие ключи категорий для callback_data.
#
# БЫЛ БАГ: в callback_data подставлялось полное название региона
# («resources_region:mvd_max:Ханты-Мансийский автономный округ — Югра»).
# Telegram ограничивает callback_data 64 БАЙТАМИ, а кириллица в UTF-8
# занимает по 2 байта на символ — такие кнопки просто не создавались,
# и меню по регионам падало с ошибкой. Теперь в callback_data идут
# короткий ключ категории и ЧИСЛОВОЙ индекс региона.
_CATEGORY_KEYS = {
    "tg": ResourceCategory.MVD_TELEGRAM.value,
    "max": ResourceCategory.MVD_MAX.value,
    "edu": ResourceCategory.INSTITUTE.value,
}
_CATEGORY_BY_VALUE = {v: k for k, v in _CATEGORY_KEYS.items()}

_CATEGORY_LABELS = {
    ResourceCategory.MVD_TELEGRAM.value: "📢 МВД в Telegram",
    ResourceCategory.MVD_MAX.value: "🅼 МВД в MAX",
    ResourceCategory.INSTITUTE.value: "🎓 Вузы МВД России",
}

REGIONS_PER_PAGE = 8


def _public_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"res:cat:{key}")]
        for key, value in _CATEGORY_KEYS.items()
        for label in [_CATEGORY_LABELS[value]]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _category_keyboard(key: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Меню одной категории: федеральные источники + регионы страницами."""
    category = _CATEGORY_KEYS[key]
    regions = db.list_resource_regions(settings.db_path, category)
    federal = db.list_resources(settings.db_path, category=category, federal_only=True)

    rows: list[list[InlineKeyboardButton]] = []

    for resource in federal[:6]:
        rows.append([InlineKeyboardButton(text=f"⭐ {resource.name}", url=resource.url)])

    total_pages = max(1, (len(regions) + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = regions[page * REGIONS_PER_PAGE:(page + 1) * REGIONS_PER_PAGE]

    for region in chunk:
        index = regions.index(region)
        rows.append([
            InlineKeyboardButton(
                text=f"📍 {region}", callback_data=f"res:reg:{key}:{index}"
            )
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀️", callback_data=f"res:cat:{key}:{page - 1}"
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="res:noop"
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="▶️", callback_data=f"res:cat:{key}:{page + 1}"
            ))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="res:home")])

    title = _CATEGORY_LABELS[category]
    if regions:
        caption = (
            f"<b>{title}</b>\n\n"
            f"Федеральные источники — кнопками выше.\n"
            f"Ниже выберите регион ({len(regions)} доступно):"
        )
    else:
        caption = f"<b>{title}</b>\n\nВыберите источник:"

    return caption, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("resources"))
async def cmd_resources(message: Message):
    """Доступно всем — это витрина для населения, не команда модерации."""
    _track(message)
    counts = db.count_resources(settings.db_path)
    total = sum(counts.values())
    await message.answer(
        "🏛 <b>Официальные источники</b>\n\n"
        f"В справочнике {total} проверенных ссылок: каналы МВД в Telegram "
        "и MAX, а также образовательные организации МВД России.\n\n"
        "Выберите категорию:",
        reply_markup=_public_menu_keyboard(),
    )


# ==========================================================================
# ГЛАВНОЕ МЕНЮ ДЛЯ ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ
# ==========================================================================
#
# Постоянная клавиатура внизу экрана: человеку не нужно помнить команды
# и лезть в меню — все разделы всегда перед глазами. Служебные команды
# (/fetch, /diag и прочие) сюда сознательно не попадают, их видят только
# сотрудники проекта в тексте /help.

BTN_SOURCES = "🏛 Источники МВД"
BTN_SITUATIONS = "🆘 Что делать, если…"
BTN_ASSISTANT = "💬 Задать вопрос"
BTN_PHONES = "☎️ Экстренные телефоны"
BTN_NEWS = "📰 Хорошие новости"
BTN_EDUCATION = "🎓 Учёба в МВД"
BTN_REGION = "📍 Мой регион"
BTN_ABOUT = "ℹ️ О проекте"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SITUATIONS), KeyboardButton(text=BTN_PHONES)],
        [KeyboardButton(text=BTN_ASSISTANT), KeyboardButton(text=BTN_SOURCES)],
        [KeyboardButton(text=BTN_NEWS), KeyboardButton(text=BTN_EDUCATION)],
        [KeyboardButton(text=BTN_REGION), KeyboardButton(text=BTN_ABOUT)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,  # иначе клавиатура занимает пол-экрана
        input_field_placeholder="Выберите раздел или напишите вопрос",
    )


def _track(message: Message) -> None:
    """
    Отмечает обращение обычного пользователя.

    Аудитория раньше нигде не учитывалась: в bot_users попадали только
    админы и модераторы, поэтому нельзя было ни посчитать пользователей,
    ни понять, какими разделами они пользуются.

    Ошибку записи глотаем сознательно: подсчёт статистики не повод
    ломать ответ пользователю.
    """
    if message.from_user is None or message.from_user.is_bot:
        return
    try:
        db.touch_audience_user(
            settings.db_path,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            language_code=message.from_user.language_code,
        )
    except Exception:
        logger.debug("Не удалось учесть пользователя", exc_info=True)


# ---------- Экстренные телефоны ----------

@dp.message(Command("phones"))
@dp.message(F.text == BTN_PHONES)
async def cmd_phones(message: Message):
    _track(message)

    lines = ["☎️ <b>Экстренные службы</b>\n"]
    for phone in EMERGENCY_PHONES:
        lines.append(f"<b>{phone.number}</b> — {phone.title}")
        if phone.note:
            lines.append(f"    <i>{phone.note}</i>")

    lines.append("\n📞 <b>Телефоны доверия</b>\n")
    for phone in TRUST_PHONES:
        lines.append(f"<b>{phone.number}</b> — {phone.title}")
        if phone.note:
            lines.append(f"    <i>{phone.note}</i>")

    lines.append(
        "\n<i>Звонок на 112 проходит без денег на счёте, без SIM-карты "
        "и при заблокированном экране телефона.</i>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆘 Что делать, если…", callback_data="sit:home")
    ]])
    await message.answer("\n".join(lines), reply_markup=keyboard)


# ---------- «Что делать, если…» ----------

def _situations_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=s.button, callback_data=f"sit:show:{s.key}")]
        for s in SITUATIONS
    ]
    rows.append([
        InlineKeyboardButton(text="☎️ Экстренные телефоны", callback_data="sit:phones")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


SITUATIONS_INTRO = (
    "🆘 <b>Что делать, если…</b>\n\n"
    "Короткие памятки по типовым ситуациям — что делать по шагам "
    "и куда обращаться.\n\n"
    "Выберите ситуацию:"
)


@dp.message(Command("situations"))
@dp.message(F.text == BTN_SITUATIONS)
async def cmd_situations(message: Message):
    _track(message)
    await message.answer(SITUATIONS_INTRO, reply_markup=_situations_keyboard())


@dp.callback_query(F.data == "sit:home")
async def on_situations_home(callback: CallbackQuery):
    await _safe_edit_markup(callback, SITUATIONS_INTRO, _situations_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "sit:phones")
async def on_situations_phones(callback: CallbackQuery):
    await callback.answer()
    await cmd_phones(callback.message)


@dp.callback_query(F.data.startswith("sit:show:"))
async def on_situation_show(callback: CallbackQuery):
    key = callback.data.split(":", 2)[2]
    situation = SITUATION_BY_KEY.get(key)

    if situation is None:
        await callback.answer("Раздел не найден.", show_alert=True)
        return

    lines = [f"<b>{situation.title}</b>\n"]
    for index, step in enumerate(situation.steps, 1):
        lines.append(f"<b>{index}.</b> {step}")

    if situation.warning:
        lines.append(f"\n⚠️ <b>Важно.</b> {situation.warning}")

    if situation.phones:
        lines.append("\n☎️ " + " · ".join(f"<b>{p}</b>" for p in situation.phones))

    lines.append(f"\n{DISCLAIMER}")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать свой вопрос", callback_data="ask:start")],
        [InlineKeyboardButton(text="⬅️ Все ситуации", callback_data="sit:home")],
    ])
    await _safe_edit_markup(callback, "\n".join(lines), keyboard)
    await callback.answer()


# ---------- ИИ-помощник ----------

class AskState(StatesGroup):
    """Ждём от пользователя текст вопроса."""
    waiting_question = State()


ASSISTANT_INTRO = (
    "💬 <b>Задайте вопрос</b>\n\n"
    "Помогу разобраться: куда обращаться, как подать заявление, "
    "какие у вас права, что делать в конкретной ситуации.\n\n"
    "Напишите вопрос обычными словами — например:\n"
    "<i>«У меня украли телефон, что делать?»</i>\n"
    "<i>«Могут ли остановить и проверить документы просто так?»</i>\n\n"
    "⚠️ Это <b>справочная информация, а не юридическая консультация</b>. "
    "Точные нормы и сроки уточняйте у юриста.\n"
    "🚨 Если опасность прямо сейчас — звоните <b>112</b> или <b>102</b>."
)


@dp.message(Command("ask"))
@dp.message(F.text == BTN_ASSISTANT)
async def cmd_ask(message: Message, state: FSMContext):
    _track(message)

    if not settings.assistant_enabled or not settings.ai_configured:
        await message.answer(
            "Помощник сейчас отключён. Загляните в раздел "
            "«🆘 Что делать, если…» — там готовые памятки по частым ситуациям.",
            reply_markup=_situations_keyboard(),
        )
        return

    # Вопрос можно задать сразу в одной строке: «/ask что делать при ДТП»
    parts = (message.text or "").split(maxsplit=1)
    if message.text and message.text.startswith("/ask") and len(parts) > 1:
        await _answer_question(message, parts[1])
        return

    allowed, remaining = await asyncio.to_thread(
        assistant.check_rate_limit, message.from_user.id
    )
    if not allowed:
        await message.answer(
            "Вы задали много вопросов за сутки — лимит исчерпан. "
            "Попробуйте завтра или загляните в «🆘 Что делать, если…»."
        )
        return

    await state.set_state(AskState.waiting_question)
    await message.answer(
        ASSISTANT_INTRO,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✖️ Отмена", callback_data="ask:cancel")
        ]]),
    )


@dp.callback_query(F.data == "ask:start")
async def on_ask_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AskState.waiting_question)
    await callback.message.answer(
        ASSISTANT_INTRO,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✖️ Отмена", callback_data="ask:cancel")
        ]]),
    )


@dp.callback_query(F.data == "ask:cancel")
async def on_ask_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.answer("Хорошо. Чем ещё помочь?",
                                  reply_markup=main_menu_keyboard())


@dp.message(AskState.waiting_question, F.text)
async def on_question_received(message: Message, state: FSMContext):
    # Нажатие кнопки главного меню — это не вопрос, а переход в раздел.
    if message.text in _MENU_BUTTONS:
        await state.clear()
        await _dispatch_menu_button(message, state)
        return

    await state.clear()
    await _answer_question(message, message.text)


async def _answer_question(
    message: Message, question: str, user_id: int | None = None
) -> None:
    """
    user_id передаётся явно, когда ответ запускается из callback: там
    message — сообщение бота, и message.from_user.id указывал бы на
    самого бота (лимит запросов считался бы не тому человеку).
    """
    _track(message)

    if user_id is None and message.from_user and not message.from_user.is_bot:
        user_id = message.from_user.id
    if user_id is None:
        return

    allowed, remaining = await asyncio.to_thread(
        assistant.check_rate_limit, user_id
    )
    if not allowed:
        await message.answer(
            "Вы задали много вопросов за сутки — лимит исчерпан. "
            "Попробуйте завтра или загляните в «🆘 Что делать, если…».",
            reply_markup=main_menu_keyboard(),
        )
        return

    thinking = await message.answer("💭 Читаю вопрос…")

    # Запрос к модели идёт в отдельном потоке: он блокирующий и на
    # несколько секунд подвесил бы весь бот для остальных пользователей.
    answer = await asyncio.to_thread(
        assistant.ask_assistant, question, user_id
    )

    with contextlib.suppress(Exception):
        await thinking.delete()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ещё вопрос", callback_data="ask:start")],
        [InlineKeyboardButton(text="🆘 Готовые памятки", callback_data="sit:home")],
    ])

    text = answer.text
    if answer.ok and not answer.refused and remaining <= 5:
        text += f"\n\n<i>Осталось вопросов на сегодня: {remaining - 1}</i>"

    await message.answer(_fit_html(text, MESSAGE_LIMIT), reply_markup=keyboard)


# ---------- Хорошие новости ----------

@dp.message(Command("news"))
@dp.message(F.text == BTN_NEWS)
async def cmd_news(message: Message, user_id: int | None = None):
    """
    user_id передаётся явно, когда раздел открыт кнопкой из callback:
    там message — это сообщение БОТА, и message.from_user.id вернул бы
    идентификатор самого бота, а не человека (тогда регион всегда
    оказывался бы пустым).
    """
    _track(message)

    if user_id is None and message.from_user and not message.from_user.is_bot:
        user_id = message.from_user.id

    region = db.get_audience_region(settings.db_path, user_id) if user_id else None
    items = db.recent_published(settings.db_path, limit=5, region=region)

    # Если по выбранному региону постов ещё нет — показываем федеральные,
    # иначе человек увидит пустой раздел и решит, что бот сломан.
    fallback_used = False
    if not items and region:
        items = db.recent_published(settings.db_path, limit=5)
        fallback_used = True

    if not items:
        await message.answer(
            "📰 Пока нет опубликованных новостей.\n\n"
            f"Следите за каналом: https://t.me/{settings.channel_username}"
        )
        return

    header = "📰 <b>Последние хорошие новости</b>"
    if region and not fallback_used:
        header += f"\n<i>Ваш регион: {html.escape(region)}</i>"
    elif fallback_used:
        header += f"\n<i>По региону «{html.escape(region)}» постов пока нет — показываю все</i>"

    await message.answer(header)

    for item in items:
        body = build_post_text(item)
        place = " · ".join(p for p in (item.city, item.region) if p)
        text = (f"📍 <b>{html.escape(place)}</b>\n\n" if place else "") + body

        if item.image_path and image_pipeline.image_exists(item.image_path):
            try:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=FSInputFile(resolve(item.image_path)),
                    caption=_fit_html(text, CAPTION_LIMIT),
                )
                await asyncio.sleep(0.3)
                continue
            except Exception:
                logger.debug("Не удалось отправить фото новости %s", item.id)

        await message.answer(_fit_html(text, MESSAGE_LIMIT))
        await asyncio.sleep(0.3)

    await message.answer(
        f"Все новости — в канале: https://t.me/{settings.channel_username}",
        reply_markup=main_menu_keyboard(),
    )


# ---------- Учёба в МВД ----------

@dp.message(Command("education"))
@dp.message(F.text == BTN_EDUCATION)
async def cmd_education(message: Message):
    _track(message)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🎓 Список вузов по регионам", callback_data="res:cat:edu"
        )
    ]])
    await message.answer(EDUCATION_INFO, reply_markup=keyboard)


# ---------- Мой регион ----------

REGIONS_PER_PAGE_USER = 8


def _user_region_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    """
    Выбор своего региона. Регионы берём из справочника MAX-каналов —
    он самый полный (85 субъектов), поэтому у пользователя почти
    наверняка найдётся свой.
    """
    regions = db.list_resource_regions(
        settings.db_path, ResourceCategory.MVD_MAX.value
    )
    total_pages = max(1, (len(regions) + REGIONS_PER_PAGE_USER - 1) // REGIONS_PER_PAGE_USER)
    page = max(0, min(page, total_pages - 1))
    chunk = regions[page * REGIONS_PER_PAGE_USER:(page + 1) * REGIONS_PER_PAGE_USER]

    rows = [
        [InlineKeyboardButton(
            text=f"📍 {region}", callback_data=f"myreg:set:{regions.index(region)}"
        )]
        for region in chunk
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"myreg:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="res:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"myreg:page:{page + 1}"))
    rows.append(nav)

    rows.append([InlineKeyboardButton(text="🗑 Сбросить регион", callback_data="myreg:clear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("region"))
@dp.message(F.text == BTN_REGION)
async def cmd_region(message: Message):
    _track(message)

    current = db.get_audience_region(settings.db_path, message.from_user.id)
    header = "📍 <b>Ваш регион</b>\n\n"
    if current:
        header += f"Сейчас выбран: <b>{html.escape(current)}</b>\n\n"
    header += (
        "Регион нужен, чтобы показывать вам каналы местного управления МВД "
        "и новости вашего края в первую очередь.\n\nВыберите регион:"
    )

    await message.answer(header, reply_markup=_user_region_keyboard())


@dp.callback_query(F.data.startswith("myreg:page:"))
async def on_region_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[2])
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=_user_region_keyboard(page))
    await callback.answer()


@dp.callback_query(F.data.startswith("myreg:set:"))
async def on_region_set(callback: CallbackQuery):
    regions = db.list_resource_regions(
        settings.db_path, ResourceCategory.MVD_MAX.value
    )
    try:
        region = regions[int(callback.data.split(":")[2])]
    except (ValueError, IndexError):
        await callback.answer("Регион не найден — откройте меню заново.", show_alert=True)
        return

    db.touch_audience_user(settings.db_path, callback.from_user.id,
                           callback.from_user.username, callback.from_user.full_name)
    db.set_audience_region(settings.db_path, callback.from_user.id, region)

    await callback.answer(f"Регион: {region}")
    await _show_region_card(callback.message, region)


@dp.callback_query(F.data == "myreg:clear")
async def on_region_clear(callback: CallbackQuery):
    db.set_audience_region(settings.db_path, callback.from_user.id, None)
    await callback.answer("Регион сброшен")
    await _safe_edit_markup(
        callback,
        "📍 Регион сброшен. Теперь бот показывает информацию по всей стране.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📍 Выбрать регион", callback_data="myreg:page:0")
        ]]),
    )


async def _show_region_card(message: Message, region: str) -> None:
    """Всё, что бот знает о выбранном регионе, одним сообщением."""
    rows: list[list[InlineKeyboardButton]] = []

    for category in (
        ResourceCategory.MVD_MAX.value,
        ResourceCategory.MVD_TELEGRAM.value,
        ResourceCategory.INSTITUTE.value,
    ):
        for resource in db.list_resources(
            settings.db_path, category=category, region=region
        )[:4]:
            prefix = {
                ResourceCategory.MVD_MAX.value: "🅼",
                ResourceCategory.MVD_TELEGRAM.value: "📢",
                ResourceCategory.INSTITUTE.value: "🎓",
            }[category]
            rows.append([
                InlineKeyboardButton(text=f"{prefix} {resource.name}", url=resource.url)
            ])

    news_count = len(db.recent_published(settings.db_path, limit=50, region=region))
    if news_count:
        rows.append([
            InlineKeyboardButton(text="📰 Новости региона", callback_data="myreg:news")
        ])
    rows.append([
        InlineKeyboardButton(text="📍 Сменить регион", callback_data="myreg:page:0")
    ])

    text = f"📍 <b>{html.escape(region)}</b>\n\n"
    if rows:
        text += "Официальные источники вашего региона:"
    else:
        text += (
            "Пока не нашёл источников по этому региону. "
            "Федеральные каналы доступны в разделе «🏛 Источники МВД»."
        )

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data == "myreg:news")
async def on_region_news(callback: CallbackQuery):
    await callback.answer()
    await cmd_news(callback.message, user_id=callback.from_user.id)


# ---------- О проекте ----------

@dp.message(Command("about"))
@dp.message(F.text == BTN_ABOUT)
async def cmd_about(message: Message):
    _track(message)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Читать канал",
            url=f"https://t.me/{settings.channel_username}",
        )],
        [InlineKeyboardButton(text="🏛 Официальные источники", callback_data="res:home")],
    ])
    await message.answer(ABOUT_TEXT, reply_markup=keyboard)


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    """Вернуть клавиатуру, если пользователь её свернул или скрыл."""
    _track(message)
    await message.answer(
        "Главное меню — выберите раздел:", reply_markup=main_menu_keyboard()
    )


# ---------- Разбор нажатий главного меню ----------

_MENU_BUTTONS = {
    BTN_SOURCES, BTN_SITUATIONS, BTN_ASSISTANT, BTN_PHONES,
    BTN_NEWS, BTN_EDUCATION, BTN_REGION, BTN_ABOUT,
}


async def _dispatch_menu_button(message: Message, state: FSMContext) -> None:
    """Вызывает обработчик раздела по надписи на кнопке."""
    text = message.text
    if text == BTN_SOURCES:
        await cmd_resources(message)
    elif text == BTN_SITUATIONS:
        await cmd_situations(message)
    elif text == BTN_ASSISTANT:
        await cmd_ask(message, state)
    elif text == BTN_PHONES:
        await cmd_phones(message)
    elif text == BTN_NEWS:
        await cmd_news(message)
    elif text == BTN_EDUCATION:
        await cmd_education(message)
    elif text == BTN_REGION:
        await cmd_region(message)
    elif text == BTN_ABOUT:
        await cmd_about(message)


@dp.callback_query(F.data == "res:home")
async def on_resources_home(callback: CallbackQuery):
    await _safe_edit_markup(
        callback,
        "🏛 <b>Официальные источники</b>\n\nВыберите категорию:",
        _public_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("res:cat:"))
async def on_resources_category(callback: CallbackQuery):
    parts = callback.data.split(":")
    key = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0

    if key not in _CATEGORY_KEYS:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return

    caption, keyboard = _category_keyboard(key, page)
    await _safe_edit_markup(callback, caption, keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("res:reg:"))
async def on_resources_region(callback: CallbackQuery):
    _, _, key, raw_index = callback.data.split(":", 3)
    category = _CATEGORY_KEYS.get(key)
    if not category:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return

    regions = db.list_resource_regions(settings.db_path, category)
    try:
        region = regions[int(raw_index)]
    except (ValueError, IndexError):
        await callback.answer("Регион не найден — откройте меню заново.", show_alert=True)
        return

    items = db.list_resources(settings.db_path, category=category, region=region)
    if not items:
        await callback.answer("Для этого региона пока ничего нет.", show_alert=True)
        return

    rows = [[InlineKeyboardButton(text=item.name, url=item.url)] for item in items[:12]]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"res:cat:{key}")])

    await _safe_edit_markup(
        callback,
        f"{_CATEGORY_LABELS[category]}\n📍 <b>{html.escape(region)}</b>",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data == "res:noop")
async def on_resources_noop(callback: CallbackQuery):
    await callback.answer()


async def _safe_edit_markup(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup
) -> None:
    """
    Меняет текст и клавиатуру сообщения. Если сообщение было отправлено
    как фото (edit_text для него недоступен) или содержимое не
    изменилось, тихо переживает ошибку вместо падения хендлера.
    """
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.answer(text, reply_markup=keyboard)


# ==========================================================================
# Базовые команды
# ==========================================================================

PUBLIC_GREETING = (
    "👮 <b>Хороший полицейский</b>\n\n"
    "Здесь собраны истории о том, как сотрудники полиции спасают, "
    "помогают и поддерживают людей.\n\n"
    "<b>А ещё бот поможет разобраться:</b>\n"
    "🆘 памятки «что делать, если…» — кража, ДТП, мошенники, пропал человек\n"
    "☎️ телефоны экстренных служб\n"
    "💬 вопрос своими словами — подскажу, куда идти и какие у вас права\n"
    "🏛 официальные каналы МВД в Telegram и MAX по всем регионам\n"
    "🎓 как поступить в вуз МВД\n\n"
    "Выберите раздел кнопками внизу 👇"
)

PUBLIC_HELP = (
    "👮 <b>Что умеет бот</b>\n\n"
    "<b>Разделы</b> (кнопки внизу экрана):\n"
    "🆘 <b>Что делать, если…</b> — пошаговые памятки\n"
    "☎️ <b>Экстренные телефоны</b> — 112, 102 и телефоны доверия\n"
    "💬 <b>Задать вопрос</b> — помощник ответит своими словами\n"
    "🏛 <b>Источники МВД</b> — официальные каналы по регионам\n"
    "📰 <b>Хорошие новости</b> — последние публикации\n"
    "🎓 <b>Учёба в МВД</b> — как поступить, список вузов\n"
    "📍 <b>Мой регион</b> — подобрать источники и новости по краю\n"
    "ℹ️ <b>О проекте</b>\n\n"
    "<b>Команды</b>\n"
    "/menu — вернуть кнопки, если пропали\n"
    "/situations, /phones, /ask, /resources, /news, /education, /region\n"
    "/ask &lt;вопрос&gt; — спросить сразу одной строкой\n\n"
    "🚨 Если случилось происшествие — звоните <b>102</b> или <b>112</b>."
)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    # Сбрасываем состояние: если человек ушёл из «жду вопрос» через
    # /start, бот не должен принять следующую фразу за вопрос к ИИ.
    await state.clear()
    _track(message)

    user = db.get_user(settings.db_path, message.from_user.id) if message.from_user else None

    if user is None:
        await message.answer(PUBLIC_GREETING, reply_markup=main_menu_keyboard())
        return

    # У сотрудника — и публичное меню кнопками, и служебная справка.
    await message.answer(PUBLIC_GREETING, reply_markup=main_menu_keyboard())
    await message.answer(_help_text(user.role))


@dp.message(Command("help"))
async def cmd_help(message: Message):
    _track(message)

    user = db.get_user(settings.db_path, message.from_user.id) if message.from_user else None
    if user is None:
        await message.answer(PUBLIC_HELP, reply_markup=main_menu_keyboard())
        return
    await message.answer(PUBLIC_HELP)
    await message.answer(_help_text(user.role))


def _help_text(role: UserRole) -> str:
    lines = [
        "👮 <b>Хороший полицейский</b> — служебный режим\n",
        "<b>Работа с новостями</b>",
        "/fetch — собрать и обработать свежие новости",
        "/pending — показать очередь модерации",
        "/stats — статистика работы бота",
        "/audience — статистика по пользователям бота",
        "/schedule_status — статус автосбора",
        "",
        "<b>Общее</b>",
        "/resources — официальные источники",
        "/whoami — ваша роль",
        "/chatid — идентификатор текущего чата",
    ]
    if role == UserRole.ADMIN:
        lines += [
            "",
            "<b>Автосбор</b>",
            "/schedule_on — включить по расписанию",
            "/schedule_off — выключить",
            "/set_interval &lt;минуты&gt; — интервал (минимум 10)",
            "",
            "<b>Администрирование</b>",
            "/diag — диагностика настроек и подсистем",
            "/add_moderator — ответом на сообщение, или @username / id",
            "/remove_moderator — ответом на сообщение, или id",
            "/list_moderators — кто имеет доступ",
            "/clear — архивировать очередь модерации",
            "/cleanup_images — удалить неиспользуемые картинки",
            "/assistant_log — последние вопросы к ИИ-помощнику",
            "/broadcast &lt;текст&gt; — рассылка по всем пользователям бота",
            "",
            "<b>Справочник источников</b>",
            "/add_resource категория | Название | https://ссылка | Регион",
            "   категории: mvd_telegram, mvd_max, institute",
            "/list_resources [категория] — список с id",
            "/remove_resource &lt;id&gt;",
            "/reseed — перезалить встроенный справочник",
        ]
    return "\n".join(lines)


@dp.message(Command("whoami"))
async def cmd_whoami(message: Message):
    user = db.get_user(settings.db_path, message.from_user.id)
    if user is None:
        await message.answer(
            "У вас нет служебной роли в этом боте — доступны публичные "
            "разделы: /resources"
        )
        return
    icon = "👑" if user.role == UserRole.ADMIN else "🛡"
    await message.answer(f"{icon} Ваша роль: <b>{user.role.value}</b>")


@dp.message(Command("chatid"))
async def cmd_chatid(message: Message):
    """Помогает настроить MODERATOR_CHAT_ID/PUBLISH_CHAT_ID без сторонних ботов."""
    await message.answer(
        f"🆔 Идентификатор этого чата: <code>{message.chat.id}</code>\n"
        f"Тип: {message.chat.type}"
    )


# ==========================================================================
# Сбор и модерация
# ==========================================================================

@dp.message(Command("fetch"))
async def cmd_fetch(message: Message):
    if not await _require_role(message, UserRole.MODERATOR):
        return

    if not settings.moderator_chat_id:
        await message.answer(
            "⚠️ MODERATOR_CHAT_ID не задан — не знаю, куда слать посты на модерацию.\n"
            "Узнать id текущего чата: /chatid"
        )
        return

    if _collection_lock.locked():
        await message.answer(
            "⏳ Сбор новостей уже выполняется (возможно, по расписанию) — подождите."
        )
        return

    status = await message.answer("⏳ Запускаю сбор новостей...")
    _mark_scheduler_run()

    try:
        items, stats = await run_collection_and_notify(status)
    except Exception as exc:
        logger.exception("Ошибка сбора новостей")
        await _safe_edit(status, f"❌ Ошибка при сборе новостей:\n<code>{html.escape(str(exc))}</code>")
        return

    header = (
        f"✅ <b>Сбор завершён</b>" if items
        else "🔍 <b>Новых подходящих новостей не найдено</b>"
    )
    await _safe_edit(status, header + "\n\n" + "\n".join(stats.summary_lines()))


@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    if not await _require_role(message, UserRole.MODERATOR):
        return

    items = db.get_by_status(settings.db_path, NewsStatus.PENDING_MODERATION)
    if not items:
        await message.answer("✨ Очередь пуста — всё обработано.")
        return

    await message.answer(f"📋 В очереди {len(items)} новостей, отправляю...")
    for item in items:
        try:
            await send_item_to_moderators(item, chat_id=message.chat.id)
        except Exception:
            logger.exception("Не удалось показать новость %s", item.id)
        # Telegram ограничивает частоту отправки — без паузы на длинной
        # очереди прилетает 429 и часть новостей теряется.
        await asyncio.sleep(0.4)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await _require_role(message, UserRole.MODERATOR):
        return

    by_status = db.count_by_status(settings.db_path)
    rejects = db.count_reject_reasons(settings.db_path, days=30)
    published_week = db.count_recent(settings.db_path, NewsStatus.PUBLISHED, days=7)

    reject_labels = {
        "negative_marker": "негатив о сотруднике",
        "off_format": "не тот формат (розыск/вакансии)",
        "too_short": "слишком короткий текст",
        "routine": "служебная рутина",
        "not_positive": "не позитивная",
        "not_authentic": "сомнительная достоверность",
        "archived": "архивировано вручную",
        "llm_or_legacy": "решение ИИ (без кода)",
    }

    lines = [
        "📊 <b>Статистика бота</b>\n",
        "<b>Очередь и архив</b>",
        f"⏳ На модерации: {by_status.get(NewsStatus.PENDING_MODERATION.value, 0)}",
        f"📤 Опубликовано всего: {by_status.get(NewsStatus.PUBLISHED.value, 0)}",
        f"📅 Опубликовано за 7 дней: {published_week}",
        f"🚫 Отклонено всего: {by_status.get(NewsStatus.REJECTED.value, 0)}",
    ]

    if rejects:
        lines.append("\n<b>Причины отказов за 30 дней</b>")
        for code, count in list(rejects.items())[:8]:
            lines.append(f"   • {reject_labels.get(code, code)}: {count}")

    counts = db.count_resources(settings.db_path)
    lines.append("\n<b>Справочник источников</b>")
    for value, label in _CATEGORY_LABELS.items():
        lines.append(f"   {label}: {counts.get(value, 0)}")

    audience = db.count_audience(settings.db_path)
    lines.append("\n<b>Пользователи бота</b>")
    lines.append(f"   Всего: {audience['total']}, активны за неделю: "
                 f"{audience['active_week']}")
    lines.append(f"   Вопросов помощнику за 30 дней: "
                 f"{db.count_assistant_usage(settings.db_path, days=30)}")
    lines.append("\n<i>Подробнее по аудитории — /audience</i>")

    await message.answer("\n".join(lines))


@dp.message(Command("diag"))
async def cmd_diag(message: Message):
    """Диагностика: что настроено, что нет, и работает ли генерация картинок."""
    if not await _require_role(message, UserRole.ADMIN):
        return

    from pipeline.image_render import CARD_RENDER_AVAILABLE

    missing = validate_settings()
    scheduler = _get_scheduler_state()

    lines = [
        "🔧 <b>Диагностика</b>\n",
        "<b>Настройки</b>",
        f"{'✅' if settings.telegram_bot_token else '❌'} Токен бота",
        f"{'✅' if settings.moderator_chat_id else '❌'} Чат модераторов: "
        f"<code>{settings.moderator_chat_id or '—'}</code>",
        f"{'✅' if settings.publish_chat_id else '❌'} Канал публикации: "
        f"<code>{settings.publish_chat_id or '—'}</code>",
        f"{'✅' if settings.ai_configured else '❌'} AI Provider: "
        f"<code>{html.escape(settings.ai_base_url)}</code>",
        f"      модель: <code>{html.escape(settings.ai_model)}</code>",
        "",
        "<b>Изображения</b>",
        f"{'✅' if CARD_RENDER_AVAILABLE else '❌'} Рендер карточек (Pillow)",
        f"ℹ️ Удалённая генерация: {image_pipeline.generation_status()}",
        "",
        "<b>ИИ-помощник</b>",
        f"{'✅ включён' if settings.assistant_enabled and settings.ai_configured else '❌ выключен'}"
        f" · модель <code>{html.escape(settings.assistant_model)}</code>",
        f"Лимит на человека: {settings.assistant_daily_limit or 'без лимита'} вопросов в сутки",
        "",
        "<b>Автосбор</b>",
        f"{'✅ включён' if scheduler.get('enabled') else '⏸ выключен'}, "
        f"интервал {scheduler.get('interval_minutes')} мин.",
        f"Последний запуск: {scheduler.get('last_run_at') or 'ещё не было'}",
        "",
        "<b>Источники</b>",
        f"RSS-лент: {len(settings.rss_sources)}, "
        f"Telegram-каналов: {len(settings.telegram_channels)}",
    ]

    if missing:
        lines.append("\n⚠️ <b>Не задано:</b>")
        lines += [f"   • {html.escape(m)}" for m in missing]

    await message.answer("\n".join(lines))


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    # Действие затрагивает всю очередь — требуем admin с самого начала,
    # чтобы не показывать модератору команду, которую он не сможет
    # подтвердить (раньше /clear был доступен модератору, а
    # /clear_confirm — только админу, что выглядело как поломка).
    if not await _require_role(message, UserRole.ADMIN):
        return

    pending = db.get_by_status(settings.db_path, NewsStatus.PENDING_MODERATION)
    if not pending:
        await message.answer("🧹 Очередь уже пуста.")
        return

    await message.answer(
        f"⚠️ В очереди сейчас <b>{len(pending)}</b> постов.\n\n"
        "Команда архивирует их все. Записи останутся в базе, поэтому "
        "повторно в /fetch они не попадут.\n\n"
        "Для подтверждения отправьте /clear_confirm"
    )


@dp.message(Command("clear_confirm"))
async def cmd_clear_confirm(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    count = db.archive_all_pending(settings.db_path)
    await message.answer(
        f"✅ <b>Очередь очищена</b>\n\n"
        f"📦 Архивировано: {count}\n"
        f"📰 В очереди: 0"
    )


@dp.message(Command("cleanup_images"))
async def cmd_cleanup_images(message: Message):
    """Удаляет картинки, на которые больше никто не ссылается."""
    if not await _require_role(message, UserRole.ADMIN):
        return

    known = db.all_image_paths(settings.db_path)
    removed = await asyncio.to_thread(image_pipeline.cleanup_orphan_images, known)
    await message.answer(f"🧹 Удалено неиспользуемых изображений: {removed}")


# ==========================================================================
# Кнопки модерации
# ==========================================================================

def _moderator_label(callback: CallbackQuery) -> str:
    user = callback.from_user
    if user.username:
        return f"@{html.escape(user.username)}"
    return html.escape(user.full_name or str(user.id))


async def mark_moderation_message(callback: CallbackQuery, suffix: str) -> None:
    """
    Дописывает статус к сообщению модерации, независимо от того, было это
    фото (send_photo + caption) или обычное текстовое сообщение: у фото
    текст правится только через edit_caption, а не edit_text.

    Клавиатура убирается, чтобы по обработанной новости нельзя было
    нажать кнопку повторно.

    Разметку суффикса теперь применяет parse_mode по умолчанию из
    DefaultBotProperties — раньше его здесь не было, и модератор видел
    в чате буквальный текст «<b>ОПУБЛИКОВАНО</b>» вместе с тегами.
    """
    message = callback.message
    try:
        if message.photo:
            current = (
                html_decoration.unparse(message.caption, message.caption_entities or [])
                if message.caption else ""
            )
            await message.edit_caption(
                caption=_fit_html(current + suffix, CAPTION_LIMIT),
                reply_markup=None,
            )
        else:
            current = (
                html_decoration.unparse(message.text, message.entities or [])
                if message.text else ""
            )
            await message.edit_text(
                _fit_html(current + suffix, MESSAGE_LIMIT), reply_markup=None
            )
    except Exception:
        logger.exception("Не удалось обновить сообщение модерации")
        # Не даём упасть всему хендлеру — хотя бы снимаем клавиатуру,
        # чтобы модератор не жал кнопку на «мёртвом» сообщении.
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    if not await _require_role_callback(callback, UserRole.MODERATOR):
        return

    item_id = callback.data.split(":", 1)[1]
    item = db.get_by_id(settings.db_path, item_id)

    if not item:
        await callback.answer("Новость не найдена в базе.", show_alert=True)
        return

    if not settings.publish_chat_id:
        await callback.answer(
            "PUBLISH_CHAT_ID не задан — некуда публиковать.", show_alert=True
        )
        return

    # Атомарно «занимаем» новость. Если её уже обработали (второй
    # модератор или повторное нажатие), claim вернёт False и мы НЕ
    # опубликуем пост второй раз.
    claimed = db.claim_for_moderation(
        settings.db_path, item_id, NewsStatus.APPROVED, callback.from_user.id
    )
    if not claimed:
        current = db.get_by_id(settings.db_path, item_id)
        state = current.status.value if current else "неизвестно"
        await callback.answer(
            f"Новость уже обработана (статус: {state}).", show_alert=True
        )
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        return

    await callback.answer("Публикую...")

    delivery = TelegramDelivery(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.publish_chat_id,
    )
    result = await asyncio.to_thread(
        delivery.send_post_detailed, build_post_text(item), item.image_path
    )

    if result.ok:
        db.update_status(settings.db_path, item_id, NewsStatus.PUBLISHED)
        db.set_published_message_id(settings.db_path, item_id, result.message_id)
        await mark_moderation_message(
            callback,
            f"\n\n✅ <b>ОПУБЛИКОВАНО</b> · {_moderator_label(callback)}",
        )
        logger.info("Новость %s опубликована пользователем %s",
                    item_id, callback.from_user.id)
    else:
        # Публикация не удалась — возвращаем новость в очередь, иначе она
        # осталась бы в статусе approved и потерялась для модераторов.
        db.update_status(settings.db_path, item_id, NewsStatus.PENDING_MODERATION)
        await callback.answer(
            f"Не удалось опубликовать: {result.error[:180]}", show_alert=True
        )
        logger.error("Публикация новости %s не удалась: %s", item_id, result.error)


@dp.callback_query(F.data.startswith("reject:"))
async def on_reject(callback: CallbackQuery):
    if not await _require_role_callback(callback, UserRole.MODERATOR):
        return

    item_id = callback.data.split(":", 1)[1]

    claimed = db.claim_for_moderation(
        settings.db_path, item_id, NewsStatus.REJECTED, callback.from_user.id
    )
    if not claimed:
        item = db.get_by_id(settings.db_path, item_id)
        if item is None:
            await callback.answer("Новость не найдена в базе.", show_alert=True)
            return
        await callback.answer(
            f"Новость уже обработана (статус: {item.status.value}).", show_alert=True
        )
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        return

    await mark_moderation_message(
        callback, f"\n\n❌ <b>ОТКЛОНЕНО</b> · {_moderator_label(callback)}"
    )
    await callback.answer("Отклонено")


@dp.callback_query(F.data.startswith("reimg:"))
async def on_regenerate_image(callback: CallbackQuery):
    """
    Перерисовывает изображение: полезно, когда фото из источника не
    подошло (коллаж, чужой водяной знак, посторонние люди в кадре).
    """
    if not await _require_role_callback(callback, UserRole.MODERATOR):
        return

    item_id = callback.data.split(":", 1)[1]
    item = db.get_by_id(settings.db_path, item_id)

    if item is None:
        await callback.answer("Новость не найдена.", show_alert=True)
        return

    if item.status != NewsStatus.PENDING_MODERATION:
        await callback.answer(
            f"Новость уже обработана (статус: {item.status.value}).", show_alert=True
        )
        return

    await callback.answer("Рисую новую картинку...")

    updated = await asyncio.to_thread(regenerate_image, item_id, True)
    if updated is None or not updated.image_path:
        await callback.answer("Не удалось создать изображение.", show_alert=True)
        return

    # Старое сообщение заменяем новым: подменить фото у уже отправленного
    # сообщения можно только через editMessageMedia, и это ломает подпись
    # с клавиатурой — проще прислать свежую карточку и убрать старую.
    with contextlib.suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    with contextlib.suppress(Exception):
        await callback.message.delete()

    await send_item_to_moderators(updated, chat_id=callback.message.chat.id)


# ==========================================================================
# Администрирование пользователей
# ==========================================================================

@dp.message(Command("add_moderator"))
async def cmd_add_moderator(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user
    else:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Использование: ответьте этой командой на сообщение пользователя "
                "в чате, либо укажите /add_moderator @username или telegram_id."
            )
            return
        raw = parts[1].strip().lstrip("@")
        try:
            target = await bot.get_chat(int(raw) if raw.isdigit() else f"@{raw}")
        except Exception:
            await message.answer(
                "Не удалось найти пользователя. Убедитесь, что юзернейм верный "
                "и профиль публичный — либо ответьте этой командой на его "
                "сообщение (работает всегда, независимо от настроек приватности)."
            )
            return

    if target is None:
        await message.answer("Не удалось определить пользователя.")
        return

    db.upsert_user(
        settings.db_path,
        BotUser(
            telegram_id=target.id,
            role=UserRole.MODERATOR,
            username=getattr(target, "username", None),
            full_name=getattr(target, "full_name", None) or getattr(target, "first_name", None),
            added_by=message.from_user.id,
        ),
    )
    label = f"@{target.username}" if getattr(target, "username", None) else str(target.id)
    await message.answer(f"✅ {html.escape(label)} назначен модератором.")


@dp.message(Command("remove_moderator"))
async def cmd_remove_moderator(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
    else:
        parts = message.text.split(maxsplit=1)
        raw = parts[1].strip().lstrip("@") if len(parts) > 1 else ""
        if not raw.isdigit():
            await message.answer(
                "Использование: ответьте этой командой на сообщение пользователя, "
                "либо укажите /remove_moderator &lt;telegram_id&gt; "
                "(узнать id — /list_moderators)."
            )
            return
        target_id = int(raw)

    existing = db.get_user(settings.db_path, target_id)
    if existing is None:
        await message.answer("Этот пользователь и так не имеет роли в боте.")
        return

    if target_id in settings.bot_admin_ids:
        await message.answer(
            "⛔ Нельзя снять роль с администратора, заданного через переменную "
            "окружения BOT_ADMIN_IDS — измените переменную и перезапустите бота."
        )
        return

    db.remove_user(settings.db_path, target_id)
    await message.answer(f"✅ Роль снята с <code>{target_id}</code>.")


@dp.message(Command("list_moderators"))
async def cmd_list_moderators(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    users = db.list_users(settings.db_path)
    if not users:
        await message.answer("Список пуст.")
        return

    lines = []
    for user in users:
        label = f"@{user.username}" if user.username else (user.full_name or "без имени")
        icon = "👑" if user.role == UserRole.ADMIN else "🛡"
        lines.append(
            f"{icon} {html.escape(str(label))} — <code>{user.telegram_id}</code>"
        )

    await message.answer("👥 <b>Доступ к боту</b>\n\n" + "\n".join(lines))


# ==========================================================================
# Планировщик — команды
# ==========================================================================

@dp.message(Command("schedule_on"))
async def cmd_schedule_on(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    state = _get_scheduler_state()
    state["enabled"] = True
    _set_scheduler_state(state)
    await message.answer(
        f"✅ Автосбор включён. Интервал: {state['interval_minutes']} мин.\n"
        f"Изменить: /set_interval &lt;минуты&gt;"
    )


@dp.message(Command("schedule_off"))
async def cmd_schedule_off(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    state = _get_scheduler_state()
    state["enabled"] = False
    _set_scheduler_state(state)
    await message.answer("⏸ Автосбор выключен. Ручной /fetch по-прежнему работает.")


@dp.message(Command("schedule_status"))
async def cmd_schedule_status(message: Message):
    if not await _require_role(message, UserRole.MODERATOR):
        return

    state = _get_scheduler_state()
    status = "включён ✅" if state.get("enabled") else "выключен ⏸"
    interval = state.get("interval_minutes", settings.default_post_interval_minutes)

    last_run = state.get("last_run_at")
    if last_run:
        try:
            moment = datetime.fromisoformat(last_run)
            next_run = moment + timedelta(minutes=interval)
            last_label = moment.strftime("%d.%m.%Y %H:%M UTC")
            next_label = next_run.strftime("%d.%m.%Y %H:%M UTC")
        except ValueError:
            last_label, next_label = last_run, "—"
    else:
        last_label, next_label = "ещё не запускался", "при следующей проверке"

    await message.answer(
        f"🕒 <b>Автосбор</b>\n\n"
        f"Состояние: {status}\n"
        f"Интервал: {interval} мин.\n"
        f"Последний запуск: {last_label}\n"
        f"Следующий: {next_label}"
    )


@dp.message(Command("set_interval"))
async def cmd_set_interval(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer(
            "Использование: /set_interval &lt;минуты&gt;, например /set_interval 180"
        )
        return

    minutes = int(parts[1].strip())
    if minutes < MIN_INTERVAL_MINUTES:
        await message.answer(
            f"Минимальный интервал — {MIN_INTERVAL_MINUTES} минут "
            f"(чтобы не нагружать источники и AI Provider)."
        )
        return

    state = _get_scheduler_state()
    state["interval_minutes"] = minutes
    _set_scheduler_state(state)
    await message.answer(f"✅ Интервал автосбора: {minutes} мин.")


# ==========================================================================
# Справочник источников — администрирование
# ==========================================================================

@dp.message(Command("add_resource"))
async def cmd_add_resource(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or "|" not in parts[1]:
        await message.answer(
            "Использование:\n"
            "<code>/add_resource категория | Название | https://ссылка | Регион</code>\n\n"
            "Регион необязателен — без него источник считается федеральным.\n"
            "Категории: " + ", ".join(c.value for c in ResourceCategory)
        )
        return

    fields = [p.strip() for p in parts[1].split("|")]
    if len(fields) < 3 or not all(fields[:3]):
        await message.answer("Нужно минимум: категория | Название | Ссылка")
        return

    category, name, url = fields[0], fields[1], fields[2]
    region = fields[3] if len(fields) > 3 and fields[3] else None

    if category not in {c.value for c in ResourceCategory}:
        await message.answer(
            f"Неизвестная категория «{html.escape(category)}». Доступные: "
            + ", ".join(c.value for c in ResourceCategory)
        )
        return

    if not url.startswith(("http://", "https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return

    db.add_resource(
        settings.db_path,
        PublicResource(
            id=uuid.uuid4().hex[:12],
            category=category,
            region=region,
            name=name,
            url=url,
            added_by=message.from_user.id,
        ),
    )
    await message.answer(
        f"✅ Добавлено в «{_CATEGORY_LABELS.get(category, category)}»: "
        f"{html.escape(name)}"
    )


@dp.message(Command("list_resources"))
async def cmd_list_resources_admin(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    parts = message.text.split(maxsplit=1)
    category = parts[1].strip() if len(parts) > 1 else None

    items = db.list_resources(settings.db_path, category=category)
    if not items:
        await message.answer("Список пуст.")
        return

    lines = [
        f"<code>{item.id}</code> | {item.region or 'федеральный'}\n"
        f"{html.escape(item.name)}\n{html.escape(item.url)}"
        for item in items
    ]

    # Разбиваем на сообщения, чтобы не упереться в лимит Telegram.
    chunk_size = 10
    for start in range(0, len(lines), chunk_size):
        await message.answer("\n\n".join(lines[start:start + chunk_size]))
        await asyncio.sleep(0.3)


@dp.message(Command("remove_resource"))
async def cmd_remove_resource(message: Message):
    if not await _require_role(message, UserRole.ADMIN):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /remove_resource &lt;id&gt; (id — см. /list_resources)"
        )
        return

    removed = db.remove_resource(settings.db_path, parts[1].strip())
    await message.answer("✅ Удалено." if removed else "❔ Источник с таким id не найден.")


@dp.message(Command("reseed"))
async def cmd_reseed(message: Message):
    """
    Перезаливает встроенный справочник, обновляя названия и ссылки
    существующих записей. Нужно после обновления data/resources_seed.py.
    """
    if not await _require_role(message, UserRole.ADMIN):
        return

    added = await asyncio.to_thread(seed_public_resources, True)
    counts = seed_counts()
    await message.answer(
        "✅ <b>Справочник обновлён</b>\n\n"
        f"Новых записей: {added}\n"
        f"Всего во встроенном списке: {sum(counts.values())}\n"
        + "\n".join(
            f"   {_CATEGORY_LABELS.get(k, k)}: {v}" for k, v in counts.items()
        )
    )


# ==========================================================================
# Запуск
# ==========================================================================

# ==========================================================================
# Аудитория и помощник — команды для сотрудников
# ==========================================================================

@dp.message(Command("audience"))
async def cmd_audience(message: Message):
    """Статистика по обычным пользователям бота."""
    if not await _require_role(message, UserRole.MODERATOR):
        return

    stats = db.count_audience(settings.db_path)
    regions = db.top_audience_regions(settings.db_path, limit=8)
    assistant_month = db.count_assistant_usage(settings.db_path, days=30)

    lines = [
        "👥 <b>Аудитория бота</b>\n",
        f"Всего пользователей: <b>{stats['total']}</b>",
        f"Активны за 7 дней: <b>{stats['active_week']}</b>",
        f"Активны за 30 дней: <b>{stats['active_month']}</b>",
        f"Указали регион: {stats['with_region']}",
        f"Заблокировали бота: {stats['blocked']}",
        "",
        f"💬 Вопросов помощнику за 30 дней: <b>{assistant_month}</b>",
    ]

    if regions:
        lines.append("\n<b>Популярные регионы</b>")
        lines += [f"   • {region} — {count}" for region, count in regions]

    await message.answer("\n".join(lines))


@dp.message(Command("assistant_log"))
async def cmd_assistant_log(message: Message):
    """
    Последние вопросы к помощнику — для выборочной проверки качества.
    Куратор должен видеть, что именно бот отвечает людям.
    """
    if not await _require_role(message, UserRole.ADMIN):
        return

    entries = db.recent_assistant_questions(settings.db_path, limit=10)
    if not entries:
        await message.answer("Вопросов пока не было.")
        return

    for entry in entries:
        asked = entry["asked_at"][:16].replace("T", " ")
        text = (
            f"🕒 {asked} · <code>{entry['telegram_id']}</code>\n\n"
            f"<b>Вопрос:</b> {html.escape(entry['question'][:400])}\n\n"
            f"<b>Ответ:</b> {(entry['answer'] or '—')[:1500]}"
        )
        await message.answer(_fit_html(text, MESSAGE_LIMIT))
        await asyncio.sleep(0.3)


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """
    Рассылка по аудитории бота.

    Отправка идёт с паузой: Telegram ограничивает примерно 30 сообщений
    в секунду, и без задержки часть рассылки просто не доставится, а бот
    словит временную блокировку. Тех, кто заблокировал бота, помечаем,
    чтобы не пытаться слать им снова.
    """
    if not await _require_role(message, UserRole.ADMIN):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: <code>/broadcast текст сообщения</code>\n\n"
            "Сообщение получат все пользователи бота. "
            "Разметка: только &lt;b&gt; и &lt;i&gt;."
        )
        return

    text = parts[1].strip()
    recipients = db.list_audience_ids(settings.db_path)

    if not recipients:
        await message.answer("Аудитория пуста — некому рассылать.")
        return

    status = await message.answer(
        f"📣 Начинаю рассылку на {len(recipients)} пользователей..."
    )

    sent = failed = blocked = 0
    for index, user_id in enumerate(recipients, 1):
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except TelegramForbiddenError:
            # Пользователь заблокировал бота — помечаем и больше не пробуем.
            db.mark_audience_blocked(settings.db_path, user_id)
            blocked += 1
        except Exception:
            failed += 1
            logger.debug("Рассылка: не доставлено %s", user_id, exc_info=True)

        await asyncio.sleep(0.05)

        if index % 50 == 0:
            await _safe_edit(
                status, f"📣 Отправлено {index} из {len(recipients)}..."
            )

    await _safe_edit(
        status,
        f"📣 <b>Рассылка завершена</b>\n\n"
        f"✅ Доставлено: {sent}\n"
        f"🚫 Заблокировали бота: {blocked}\n"
        f"⚠️ Ошибок: {failed}",
    )


# ==========================================================================
# Свободный текст — последний обработчик
# ==========================================================================
#
# Регистрируется ПОСЛЕ всех остальных: aiogram проверяет фильтры в
# порядке регистрации, поэтому команды и кнопки меню разбираются раньше,
# а сюда попадает только то, что никуда не подошло.

@dp.message(F.text & ~F.text.startswith("/"))
async def on_free_text(message: Message, state: FSMContext):
    """
    Человек просто написал что-то боту. Скорее всего это вопрос —
    предлагаем передать его помощнику, но не отправляем молча: запрос
    к модели стоит денег, и пользователь должен понимать, что произойдёт.
    """
    # В групповых чатах (в том числе в чате модераторов) молчим: иначе
    # бот будет реагировать на каждую реплику в обсуждении.
    if message.chat.type != "private":
        return

    _track(message)

    text = (message.text or "").strip()

    if text in _MENU_BUTTONS:
        await _dispatch_menu_button(message, state)
        return

    if len(text) < assistant.MIN_QUESTION_CHARS:
        await message.answer(
            "Не понял вопрос. Выберите раздел кнопками внизу "
            "или напишите подробнее.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if _is_smalltalk(text):
        await message.answer(
            "Здравствуйте! Выберите раздел кнопками внизу — "
            "или просто напишите свой вопрос, и я подскажу. 👇",
            reply_markup=main_menu_keyboard(),
        )
        return

    if not settings.assistant_enabled or not settings.ai_configured:
        await message.answer(
            "Выберите раздел кнопками внизу 👇\n\n"
            "Готовые памятки по частым ситуациям — «🆘 Что делать, если…».",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Отвечаем сразу, без промежуточного «передать вопрос помощнику?».
    #
    # Подтверждение выглядело логично, но давало непоследовательное
    # поведение: первое сообщение требовало нажать кнопку, а следующее
    # (бот уже был в состоянии ожидания вопроса) уходило модели напрямую.
    # Человек писал вопрос боту-справочнику — разумно на него ответить.
    # От случайного расхода квоты защищают фильтр приветствий выше
    # и суточный лимит.
    await state.clear()
    await _answer_question(message, text)


# Приветствия и благодарности не стоит отправлять модели: ответа по сути
# там нет, а суточный лимит вопросов расходуется.
_SMALLTALK = {
    "привет", "здравствуйте", "здравствуй", "добрый день", "доброе утро",
    "добрый вечер", "хай", "ку", "здарова", "приветствую", "салют",
    "спасибо", "благодарю", "спс", "пока", "до свидания", "ок", "окей",
    "хорошо", "понятно", "ясно", "да", "нет", "тест", "test", "привет!",
}


def _is_smalltalk(text: str) -> bool:
    normalized = text.lower().strip(" .,!?…")
    return normalized in _SMALLTALK


async def setup_bot_commands() -> None:
    """
    Регистрирует список команд в меню Telegram (кнопка «/» у поля ввода).

    Показываем только публичные команды: служебные видят сотрудники
    в /help, а обычному пользователю длинный список из /fetch и /diag
    только мешает.
    """
    from aiogram.types import BotCommand, BotCommandScopeDefault

    commands = [
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="situations", description="Что делать, если…"),
        BotCommand(command="phones", description="Экстренные телефоны"),
        BotCommand(command="ask", description="Задать вопрос"),
        BotCommand(command="resources", description="Официальные источники МВД"),
        BotCommand(command="news", description="Хорошие новости"),
        BotCommand(command="education", description="Учёба в МВД"),
        BotCommand(command="region", description="Выбрать свой регион"),
        BotCommand(command="about", description="О проекте"),
        BotCommand(command="help", description="Справка"),
    ]

    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        logger.info("Меню команд Telegram обновлено (%d команд)", len(commands))
    except Exception:
        # Не критично: бот работает и без зарегистрированного меню.
        logger.warning("Не удалось обновить меню команд", exc_info=True)


async def main() -> None:
    if bot is None:
        logger.error(
            "TELEGRAM_BOT_TOKEN не задан — бот не может запуститься.\n"
            "Скопируйте .env.example в .env и заполните значения."
        )
        sys.exit(1)

    validate_settings()
    db.init_db(settings.db_path)
    bootstrap_admins()
    seed_public_resources()

    await setup_bot_commands()

    me = await bot.get_me()
    logger.info("Бот @%s запущен, ожидаю команды...", me.username)

    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task

        from pipeline.ai_client import close_client
        close_client()
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено пользователем.")
