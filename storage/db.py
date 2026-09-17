"""
SQLite-хранилище, общее для Telegram-бота и веб-панели модерации: оба
процесса читают/пишут один файл БД, поэтому пост, обработанный в одном
интерфейсе, сразу виден в другом.

Что изменилось по сравнению с первой версией и зачем:

  * WAL + busy_timeout. Раньше бот и панель, обращаясь к базе
    одновременно, ловили "database is locked" и падали в обработчике.
    WAL разводит читателей и писателя, а busy_timeout заставляет
    подождать вместо мгновенной ошибки.
  * Индексы по status/source_url/fingerprint — без них каждый /pending
    и каждая проверка дубликата читали таблицу целиком.
  * Колонка fingerprint. Проверка «не было ли такой новости» раньше
    выгружала ВСЕ raw_text из базы и сравнивала множества слов в Python:
    O(n) тяжёлых операций на каждую новость, то есть O(n²) на цикл.
    Теперь у каждой новости есть отпечаток по значимым словам, поиск
    похожей идёт по индексу, а полное сравнение выполняется только для
    записей с совпавшим отпечатком.
  * Миграции. Схема меняется, а база у заказчика уже с данными — новые
    колонки добавляются через ALTER TABLE при старте.

При росте нагрузки (несколько воркеров, конкурентная запись) стоит
перейти на Postgres — интерфейс функций ниже специально «плоский»,
чтобы замена не требовала переписывать вызывающий код.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from logging_setup import get_logger
from storage.models import BotUser, NewsItem, NewsStatus, PublicResource, UserRole

logger = get_logger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    source_name TEXT,
    source_url TEXT,
    title TEXT,
    raw_text TEXT,
    published_at TEXT,
    status TEXT,
    region TEXT,
    city TEXT,
    rewritten_text TEXT,
    image_path TEXT,
    image_source TEXT,
    verification_notes TEXT,
    rejection_reason TEXT,
    tags TEXT
);
"""

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    role TEXT NOT NULL,
    added_by INTEGER,
    added_at TEXT NOT NULL
);
"""

CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

RESOURCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS public_resources (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    region TEXT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    added_by INTEGER,
    added_at TEXT NOT NULL
);
"""

# Обычные пользователи бота (НЕ сотрудники проекта).
#
# Раньше такой таблицы не было вовсе: в bot_users попадали только
# админы и модераторы, а про аудиторию бот не знал ничего — нельзя
# было ни посчитать пользователей, ни понять, чем они пользуются.
#
# Храним минимум: идентификатор, имя для обращения и регион, который
# человек выбрал сам. Ни номеров телефонов, ни текстов личных
# сообщений здесь нет.
AUDIENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_audience (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    language_code TEXT,
    region TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    interactions INTEGER NOT NULL DEFAULT 0,
    is_blocked INTEGER NOT NULL DEFAULT 0
);
"""

# Журнал вопросов к ИИ-помощнику: нужен кураторам для выборочной
# проверки качества ответов и для подсчёта дневного лимита на человека
# (лимит в памяти процесса обнулялся бы при каждом перезапуске бота).
ASSISTANT_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    asked_at TEXT NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0
);
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_news_status ON news_items(status)",
    "CREATE INDEX IF NOT EXISTS idx_news_source_url ON news_items(source_url)",
    "CREATE INDEX IF NOT EXISTS idx_news_fingerprint ON news_items(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_news_created ON news_items(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_resources_category ON public_resources(category)",
    "CREATE INDEX IF NOT EXISTS idx_audience_last_seen ON bot_audience(last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_assistant_user_date "
    "ON assistant_log(telegram_id, asked_at)",
)

# Колонки, добавленные после первого релиза: (таблица, колонка, тип).
MIGRATIONS = (
    ("news_items", "fingerprint", "TEXT"),
    ("news_items", "reject_code", "TEXT"),
    ("news_items", "created_at", "TEXT"),
    ("news_items", "moderated_by", "INTEGER"),
    ("news_items", "moderated_at", "TEXT"),
    ("news_items", "published_message_id", "INTEGER"),
    ("public_resources", "sort_order", "INTEGER"),
)


@contextmanager
def get_connection(db_path: str):
    """
    Соединение с базой с корректной обработкой ошибок.

    Раньше при исключении внутри блока транзакция не откатывалась явно:
    соединение просто закрывалось, и часть изменений могла остаться
    в неопределённом состоянии. Теперь при ошибке делаем rollback.
    """
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        # WAL позволяет читать во время записи (бот + веб-панель),
        # busy_timeout — подождать освобождения вместо "database is locked".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str):
    """Создаёт таблицы, применяет миграции и строит индексы.
    Идемпотентно — можно звать при каждом старте."""
    from paths import ensure_dirs

    ensure_dirs()

    with get_connection(db_path) as conn:
        conn.execute(SCHEMA)
        conn.execute(USERS_SCHEMA)
        conn.execute(CONFIG_SCHEMA)
        conn.execute(RESOURCES_SCHEMA)
        conn.execute(AUDIENCE_SCHEMA)
        conn.execute(ASSISTANT_LOG_SCHEMA)

        for table, column, column_type in MIGRATIONS:
            existing = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                logger.info("Миграция: добавлена колонка %s.%s", table, column)

        for index_sql in INDEXES:
            conn.execute(index_sql)

        _drop_legacy_resources(conn)

    _backfill_fingerprints(db_path)


# Первая версия бота записывала каналы МВД в Telegram с идентификаторами
# вида "mvd_tg_mediamvd". В новом справочнике (content/resources_seed.py) у
# них id "tg_mediamvd", поэтому на уже работающей базе вставка НЕ считала
# их дубликатами и в публичном меню каждый канал появлялся дважды.
LEGACY_RESOURCE_ID_PREFIX = "mvd_tg_"


def _drop_legacy_resources(conn: sqlite3.Connection) -> None:
    cursor = conn.execute(
        "DELETE FROM public_resources WHERE id LIKE ?",
        (LEGACY_RESOURCE_ID_PREFIX + "%",),
    )
    if cursor.rowcount:
        logger.info(
            "Миграция: удалено %d устаревших записей справочника (старые id)",
            cursor.rowcount,
        )


# --------------------------------------------------------------------------
# Отпечаток текста для поиска дубликатов
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# Слова, которые есть почти в каждом посте пресс-службы: если оставить
# их в отпечатке, любые две новости будут похожи друг на друга.
_STOP_WORDS = {
    "который", "которая", "которые", "после", "также", "около", "более",
    "россии", "российской", "полиции", "полицейские", "сотрудники",
    "сотрудник", "мужчина", "женщина", "человек", "области", "района",
    "города", "этом", "было", "были", "может", "если", "чтобы",
}


def _significant_words(text: str, limit: int = 40) -> list[str]:
    """Значимые слова из начала текста: длиннее 4 букв, не служебные."""
    words = _WORD_RE.findall((text or "").lower())
    result: list[str] = []
    for word in words:
        if len(word) > 4 and word not in _STOP_WORDS:
            result.append(word)
        if len(result) >= limit:
            break
    return result


def compute_fingerprint(text: str) -> str:
    """
    Отпечаток новости: 12 самых «характерных» слов начала текста,
    отсортированных и захешированных.

    Репост слово в слово даёт идентичный отпечаток, поэтому такие
    дубликаты ловятся точным сравнением по индексу, без перебора базы.
    Слегка изменённые перепечатки отпечаток не совпадёт — их дополнительно
    ловит similar_item_exists() по сохранённым словам-кандидатам.
    """
    words = sorted(set(_significant_words(text, limit=20)))[:12]
    if not words:
        return ""
    return hashlib.sha1(" ".join(words).encode("utf-8")).hexdigest()


def _backfill_fingerprints(db_path: str) -> None:
    """Проставляет отпечатки записям, созданным до появления колонки."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, raw_text FROM news_items WHERE fingerprint IS NULL LIMIT 5000"
        ).fetchall()

        if not rows:
            return

        conn.executemany(
            "UPDATE news_items SET fingerprint = ? WHERE id = ?",
            [(compute_fingerprint(row["raw_text"] or ""), row["id"]) for row in rows],
        )
        logger.info("Миграция: проставлены отпечатки для %d записей", len(rows))


# --------------------------------------------------------------------------
# Новости
# --------------------------------------------------------------------------

def save_item(db_path: str, item: NewsItem):
    data = asdict(item)
    data["published_at"] = item.published_at.isoformat()
    data["status"] = item.status.value
    data["tags"] = json.dumps(item.tags, ensure_ascii=False)
    data["fingerprint"] = compute_fingerprint(item.raw_text or "")
    data["created_at"] = datetime.now(timezone.utc).isoformat()

    with get_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO news_items (id, source_name, source_url, title, raw_text,
                published_at, status, region, city, rewritten_text, image_path,
                image_source, verification_notes, rejection_reason, tags,
                fingerprint, reject_code, created_at)
            VALUES (:id, :source_name, :source_url, :title, :raw_text,
                :published_at, :status, :region, :city, :rewritten_text, :image_path,
                :image_source, :verification_notes, :rejection_reason, :tags,
                :fingerprint, :reject_code, :created_at)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, region=excluded.region, city=excluded.city,
                rewritten_text=excluded.rewritten_text, image_path=excluded.image_path,
                image_source=excluded.image_source,
                verification_notes=excluded.verification_notes,
                rejection_reason=excluded.rejection_reason, tags=excluded.tags,
                fingerprint=excluded.fingerprint, reject_code=excluded.reject_code
        """, data)


def update_status(db_path: str, item_id: str, status: NewsStatus):
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE news_items SET status = ? WHERE id = ?",
            (status.value, item_id),
        )


def claim_for_moderation(
    db_path: str, item_id: str, new_status: NewsStatus, moderator_id: int | None = None
) -> bool:
    """
    Атомарно переводит новость из PENDING_MODERATION в новый статус.

    Возвращает True, только если переход выполнил именно этот вызов.

    ЗАЧЕМ: раньше два модератора, нажавшие «Одобрить» одновременно (или
    один человек дважды по залипшей кнопке), проходили проверку
    «новость существует» оба и публиковали пост в канал ДВАЖДЫ. Здесь
    условие status = 'pending_moderation' стоит внутри самого UPDATE,
    поэтому второй вызов изменит 0 строк и получит False.
    """
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE news_items
            SET status = ?, moderated_by = ?, moderated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                new_status.value,
                moderator_id,
                datetime.now(timezone.utc).isoformat(),
                item_id,
                NewsStatus.PENDING_MODERATION.value,
            ),
        )
        return cursor.rowcount > 0


def set_published_message_id(db_path: str, item_id: str, message_id: int | None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE news_items SET published_message_id = ? WHERE id = ?",
            (message_id, item_id),
        )


def set_image(db_path: str, item_id: str, image_path: str | None, image_source: str):
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE news_items SET image_path = ?, image_source = ? WHERE id = ?",
            (image_path, image_source, item_id),
        )


def archive_all_pending(db_path: str) -> int:
    """
    Архивирует все новости, ожидающие модерации.

    Записи остаются в БД, поэтому /fetch не будет обрабатывать их
    повторно. Возвращает количество архивированных записей.
    """
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE news_items
            SET status = ?, rejection_reason = ?, reject_code = 'archived'
            WHERE status = ?
            """,
            (
                NewsStatus.REJECTED.value,
                "Архивировано командой /clear",
                NewsStatus.PENDING_MODERATION.value,
            ),
        )
        return cursor.rowcount


def get_by_status(
    db_path: str, status: NewsStatus, limit: int | None = None
) -> list[NewsItem]:
    query = "SELECT * FROM news_items WHERE status = ? ORDER BY published_at DESC"
    params: list = [status.value]
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_item(row) for row in rows]


def recent_published(db_path: str, limit: int = 5, region: str | None = None) -> list[NewsItem]:
    """
    Последние опубликованные посты — бот показывает их пользователям
    в разделе «Хорошие новости», чтобы не заставлять уходить в канал.

    Сортировка по moderated_at (когда пост реально вышел), а не по
    published_at: у новостей из источника дата может быть любой, и
    свежеопубликованный пост оказался бы в середине списка.
    """
    query = """
        SELECT * FROM news_items
        WHERE status = ? AND rewritten_text IS NOT NULL
    """
    params: list = [NewsStatus.PUBLISHED.value]

    if region:
        query += " AND region = ?"
        params.append(region)

    query += " ORDER BY COALESCE(moderated_at, created_at, published_at) DESC LIMIT ?"
    params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_item(row) for row in rows]


def published_regions(db_path: str) -> list[str]:
    """Регионы, по которым уже есть опубликованные новости."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT region, COUNT(*) AS n FROM news_items
            WHERE status = ? AND region IS NOT NULL AND region != ''
            GROUP BY region ORDER BY n DESC
            """,
            (NewsStatus.PUBLISHED.value,),
        ).fetchall()
    return [row["region"] for row in rows]


def count_by_status(db_path: str) -> dict[str, int]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM news_items GROUP BY status"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}


def count_reject_reasons(db_path: str, days: int = 30) -> dict[str, int]:
    """Статистика отказов за период — видно, что именно отсеивает поток."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(reject_code, 'llm_or_legacy') AS code, COUNT(*) AS n
            FROM news_items
            WHERE status = ? AND (created_at IS NULL OR created_at >= ?)
            GROUP BY code ORDER BY n DESC
            """,
            (NewsStatus.REJECTED.value, since),
        ).fetchall()
    return {row["code"]: row["n"] for row in rows}


def count_recent(db_path: str, status: NewsStatus, days: int = 7) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM news_items
            WHERE status = ? AND created_at >= ?
            """,
            (status.value, since),
        ).fetchone()
    return row["n"] if row else 0


def get_by_id(db_path: str, item_id: str) -> NewsItem | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM news_items WHERE id = ?", (item_id,)
        ).fetchone()
    return _row_to_item(row) if row else None


def all_image_paths(db_path: str) -> set[str]:
    """Пути картинок, на которые ещё кто-то ссылается — для очистки диска."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT image_path FROM news_items WHERE image_path IS NOT NULL"
        ).fetchall()
    return {row["image_path"] for row in rows if row["image_path"]}


def item_exists(db_path: str, source_url: str) -> bool:
    """
    Защита от дублей по URL источника.

    БЫЛ БАГ: пустой source_url (Telegram иногда не отдаёт data-post)
    считался нормальным значением. Первая же новость с пустым URL
    сохранялась, и дальше item_exists(db, "") возвращал True для
    КАЖДОЙ следующей новости без URL — они молча пропускались все.
    """
    if not source_url:
        return False

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM news_items WHERE source_url = ? LIMIT 1", (source_url,)
        ).fetchone()
    return row is not None


def similar_item_exists(db_path: str, raw_text: str, threshold: float = 0.6) -> bool:
    """
    Защита от репостов между каналами: разные источники (например
    @mediamvd и @IrinaVolk_MVD) часто публикуют один и тот же случай
    дословно или почти дословно.

    Две ступени:
      1. Точное совпадение отпечатка — ловит дословные репосты по индексу,
         без чтения текстов.
      2. Сравнение по доле общих значимых слов — ловит перепечатки с
         правками. Здесь мы читаем тексты, но только за последние 30 дней
         и только колонку raw_text, а не всю таблицу целиком, как раньше.
    """
    fingerprint = compute_fingerprint(raw_text)
    candidate_words = set(_significant_words(raw_text))
    if not candidate_words:
        return False

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    with get_connection(db_path) as conn:
        if fingerprint:
            exact = conn.execute(
                "SELECT 1 FROM news_items WHERE fingerprint = ? LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if exact is not None:
                return True

        rows = conn.execute(
            """
            SELECT raw_text FROM news_items
            WHERE created_at IS NULL OR created_at >= ?
            ORDER BY created_at DESC
            LIMIT 800
            """,
            (since,),
        ).fetchall()

    for row in rows:
        existing_words = set(_significant_words(row["raw_text"] or ""))
        if not existing_words:
            continue
        overlap = len(candidate_words & existing_words) / len(candidate_words)
        if overlap >= threshold:
            return True

    return False


def _row_value(row: sqlite3.Row, key: str, default=None):
    """Безопасное чтение колонки, которой может не быть в старой базе."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _row_to_item(row: sqlite3.Row) -> NewsItem:
    return NewsItem(
        id=row["id"],
        source_name=row["source_name"],
        source_url=row["source_url"],
        title=row["title"],
        raw_text=row["raw_text"],
        published_at=datetime.fromisoformat(row["published_at"]),
        status=NewsStatus(row["status"]),
        region=row["region"],
        city=row["city"],
        rewritten_text=row["rewritten_text"],
        image_path=row["image_path"],
        image_source=row["image_source"],
        verification_notes=row["verification_notes"] or "",
        rejection_reason=row["rejection_reason"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        reject_code=_row_value(row, "reject_code"),
        moderated_by=_row_value(row, "moderated_by"),
        published_message_id=_row_value(row, "published_message_id"),
    )


# ---------- Пользователи бота (роли: admin / moderator) ----------

def upsert_user(db_path: str, user: BotUser):
    """Создаёт пользователя или обновляет его роль/данные, если уже есть."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bot_users (telegram_id, username, full_name, role, added_by, added_at)
            VALUES (:telegram_id, :username, :full_name, :role, :added_by, :added_at)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=COALESCE(excluded.username, bot_users.username),
                full_name=COALESCE(excluded.full_name, bot_users.full_name),
                role=excluded.role,
                added_by=excluded.added_by
            """,
            {
                "telegram_id": user.telegram_id,
                "username": user.username,
                "full_name": user.full_name,
                "role": user.role.value,
                "added_by": user.added_by,
                "added_at": user.added_at.isoformat(),
            },
        )


def get_user(db_path: str, telegram_id: int) -> BotUser | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bot_users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return _row_to_user(row) if row else None


def list_users(db_path: str) -> list[BotUser]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bot_users ORDER BY role, added_at"
        ).fetchall()
    return [_row_to_user(row) for row in rows]


def remove_user(db_path: str, telegram_id: int):
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM bot_users WHERE telegram_id = ?", (telegram_id,))


def _row_to_user(row: sqlite3.Row) -> BotUser:
    return BotUser(
        telegram_id=row["telegram_id"],
        username=row["username"],
        full_name=row["full_name"],
        role=UserRole(row["role"]),
        added_by=row["added_by"],
        added_at=datetime.fromisoformat(row["added_at"]),
    )


# ---------- Аудитория бота (обычные пользователи) ----------

def touch_audience_user(
    db_path: str,
    telegram_id: int,
    username: str | None = None,
    full_name: str | None = None,
    language_code: str | None = None,
) -> None:
    """
    Отмечает, что пользователь обратился к боту: создаёт запись при
    первом обращении и обновляет «последний визит» при каждом следующем.

    COALESCE на username/full_name: Telegram не всегда присылает эти
    поля, и без него повторный визит затирал бы уже известное имя
    значением NULL.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bot_audience (
                telegram_id, username, full_name, language_code,
                first_seen_at, last_seen_at, interactions
            )
            VALUES (:tid, :username, :full_name, :lang, :now, :now, 1)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = COALESCE(excluded.username, bot_audience.username),
                full_name = COALESCE(excluded.full_name, bot_audience.full_name),
                language_code = COALESCE(excluded.language_code,
                                         bot_audience.language_code),
                last_seen_at = excluded.last_seen_at,
                interactions = bot_audience.interactions + 1,
                is_blocked = 0
            """,
            {
                "tid": telegram_id, "username": username,
                "full_name": full_name, "lang": language_code, "now": now,
            },
        )


def set_audience_region(db_path: str, telegram_id: int, region: str | None) -> None:
    """Запоминает регион, выбранный пользователем, — чтобы не спрашивать
    его каждый раз при открытии справочника."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE bot_audience SET region = ? WHERE telegram_id = ?",
            (region, telegram_id),
        )


def get_audience_region(db_path: str, telegram_id: int) -> str | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT region FROM bot_audience WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return row["region"] if row else None


def mark_audience_blocked(db_path: str, telegram_id: int) -> None:
    """
    Пользователь заблокировал бота (Telegram сообщает об этом ошибкой
    при отправке). Помечаем, чтобы не слать ему рассылки и не считать
    его в активной аудитории.
    """
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE bot_audience SET is_blocked = 1 WHERE telegram_id = ?",
            (telegram_id,),
        )


def count_audience(db_path: str) -> dict[str, int]:
    since_week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    since_month = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    with get_connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM bot_audience").fetchone()["n"]
        blocked = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_audience WHERE is_blocked = 1"
        ).fetchone()["n"]
        week = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_audience WHERE last_seen_at >= ?",
            (since_week,),
        ).fetchone()["n"]
        month = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_audience WHERE last_seen_at >= ?",
            (since_month,),
        ).fetchone()["n"]
        with_region = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_audience WHERE region IS NOT NULL"
        ).fetchone()["n"]

    return {
        "total": total, "blocked": blocked, "active_week": week,
        "active_month": month, "with_region": with_region,
    }


def list_audience_ids(db_path: str, include_blocked: bool = False) -> list[int]:
    """Идентификаторы для рассылки. Заблокировавших бота по умолчанию
    пропускаем — им всё равно не доставится."""
    query = "SELECT telegram_id FROM bot_audience"
    if not include_blocked:
        query += " WHERE is_blocked = 0"

    with get_connection(db_path) as conn:
        return [row["telegram_id"] for row in conn.execute(query).fetchall()]


def top_audience_regions(db_path: str, limit: int = 10) -> list[tuple[str, int]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT region, COUNT(*) AS n FROM bot_audience
            WHERE region IS NOT NULL
            GROUP BY region ORDER BY n DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [(row["region"], row["n"]) for row in rows]


# ---------- Журнал ИИ-помощника ----------

def log_assistant_question(
    db_path: str, telegram_id: int, question: str,
    answer: str | None = None, asked_at: datetime | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO assistant_log (telegram_id, question, answer, asked_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                telegram_id, question, answer,
                (asked_at or datetime.now(timezone.utc)).isoformat(),
            ),
        )


def count_assistant_questions_today(db_path: str, telegram_id: int) -> int:
    """
    Сколько вопросов пользователь задал за последние сутки.

    Считаем скользящим окном в 24 часа, а не «с полуночи»: у нас нет
    часового пояса пользователя, а окно по UTC давало бы кому-то
    двойной лимит на стыке суток.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM assistant_log
            WHERE telegram_id = ? AND asked_at >= ?
            """,
            (telegram_id, since),
        ).fetchone()
    return row["n"] if row else 0


def recent_assistant_questions(db_path: str, limit: int = 20) -> list[dict]:
    """Последние вопросы — для выборочной проверки кураторами."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT telegram_id, question, answer, asked_at
            FROM assistant_log ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_assistant_usage(db_path: str, days: int = 30) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM assistant_log WHERE asked_at >= ?",
            (since,),
        ).fetchone()
    return row["n"] if row else 0


# ---------- Настройки рантайма (например, планировщик автосбора) ----------

def get_config(db_path: str, key: str, default=None):
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        logger.warning("Испорченное значение конфига %s — возвращаю default", key)
        return default


def set_config(db_path: str, key: str, value) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bot_config (key, value) VALUES (:key, :value)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            {"key": key, "value": json.dumps(value, ensure_ascii=False)},
        )


# ---------- Официальные источники для публичного меню (/resources) ----------

def add_resource(db_path: str, resource: PublicResource, update_existing: bool = False) -> None:
    """
    ON CONFLICT DO NOTHING — чтобы повторный вызов при старте бота
    (автозаполнение официальных каналов МВД) не плодил дубликаты и не
    затирал ручные правки администратора.

    update_existing=True нужен только для команды переустановки
    справочника, когда админ сознательно хочет перезаписать названия.
    """
    payload = {
        "id": resource.id,
        "category": resource.category,
        "region": resource.region,
        "name": resource.name,
        "url": resource.url,
        "added_by": resource.added_by,
        "added_at": resource.added_at.isoformat(),
    }

    conflict = (
        """
        ON CONFLICT(id) DO UPDATE SET
            category=excluded.category, region=excluded.region,
            name=excluded.name, url=excluded.url
        """
        if update_existing
        else "ON CONFLICT(id) DO NOTHING"
    )

    with get_connection(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO public_resources (id, category, region, name, url, added_by, added_at)
            VALUES (:id, :category, :region, :name, :url, :added_by, :added_at)
            {conflict}
            """,
            payload,
        )


def add_resources_bulk(
    db_path: str, resources: list[PublicResource], update_existing: bool = False
) -> int:
    """
    Пакетная вставка справочника — одна транзакция вместо сотен.

    Заполнение сотни каналов МВД и вузов по одной записи открывало и
    закрывало соединение сто раз при каждом старте бота, что заметно
    задерживало запуск.
    """
    if not resources:
        return 0

    conflict = (
        """
        ON CONFLICT(id) DO UPDATE SET
            category=excluded.category, region=excluded.region,
            name=excluded.name, url=excluded.url
        """
        if update_existing
        else "ON CONFLICT(id) DO NOTHING"
    )

    payload = [
        {
            "id": r.id, "category": r.category, "region": r.region,
            "name": r.name, "url": r.url, "added_by": r.added_by,
            "added_at": r.added_at.isoformat(),
        }
        for r in resources
    ]

    with get_connection(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM public_resources").fetchone()["n"]
        conn.executemany(
            f"""
            INSERT INTO public_resources (id, category, region, name, url, added_by, added_at)
            VALUES (:id, :category, :region, :name, :url, :added_by, :added_at)
            {conflict}
            """,
            payload,
        )
        after = conn.execute("SELECT COUNT(*) AS n FROM public_resources").fetchone()["n"]

    return after - before


def list_resources(
    db_path: str, category: str | None = None, region: str | None = None,
    federal_only: bool = False,
) -> list[PublicResource]:
    query = "SELECT * FROM public_resources"
    conditions = []
    params: list = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if federal_only:
        conditions.append("region IS NULL")
    elif region is not None:
        conditions.append("region = ?")
        params.append(region)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY region IS NOT NULL, region, name"

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_resource(row) for row in rows]


def list_resource_regions(db_path: str, category: str) -> list[str]:
    """Уникальные регионы категории — чтобы построить меню по регионам."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT region FROM public_resources
            WHERE category = ? AND region IS NOT NULL AND region != ''
            ORDER BY region
            """,
            (category,),
        ).fetchall()
    return [row["region"] for row in rows]


def count_resources(db_path: str) -> dict[str, int]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n FROM public_resources GROUP BY category"
        ).fetchall()
    return {row["category"]: row["n"] for row in rows}


def remove_resource(db_path: str, resource_id: str) -> bool:
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM public_resources WHERE id = ?", (resource_id,)
        )
        return cursor.rowcount > 0


def _row_to_resource(row: sqlite3.Row) -> PublicResource:
    return PublicResource(
        id=row["id"],
        category=row["category"],
        region=row["region"],
        name=row["name"],
        url=row["url"],
        added_by=row["added_by"],
        added_at=datetime.fromisoformat(row["added_at"]),
    )
