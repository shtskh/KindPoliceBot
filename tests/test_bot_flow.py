"""
Проверка сценариев бота без обращения к Telegram.

Как это работает: вместо запуска polling мы подсовываем боту поддельную
HTTP-сессию, которая ничего не отправляет, а записывает вызовы API, и
скармливаем диспетчеру самодельные Update — ровно такие же, какие
Telegram прислал бы при нажатии кнопки или отправке сообщения.

Так проверяется то, что юнит-тестами не поймать: правильный ли порядок
регистрации хендлеров, не перехватывает ли catch-all кнопки меню,
укладываются ли тексты в лимиты Telegram, валидна ли HTML-разметка.

Запуск:  python -m tests.test_bot_flow
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Настройки читаются при импорте config, поэтому задаём окружение раньше.
_TMP_DB = os.path.join(tempfile.gettempdir(), "kindpolice_flow_test.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.unlink(_TMP_DB + suffix)
    except OSError:
        pass

os.environ["DB_PATH"] = _TMP_DB
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
os.environ.setdefault("CHANNEL_USERNAME", "kindpolice")
os.environ["LOG_TO_FILE"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"
# По умолчанию помощник не должен трогать сеть: сценарии проверяют
# маршрутизацию, а не качество модели, и тесты обязаны проходить без
# ключей и без интернета. KP_FLOW_KEEP_AI=1 оставляет ключ на месте —
# это нужно для ручной проверки живых ответов помощника.
if os.getenv("KP_FLOW_KEEP_AI") != "1":
    os.environ["AI_PROVIDER_API_KEY"] = ""

from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import (  # noqa: E402
    TelegramMethod,
)
from aiogram.types import (  # noqa: E402
    CallbackQuery, Chat, Message, Update, User,
)

import bot as tb  # noqa: E402
from config import settings  # noqa: E402
from data.resources_seed import build_seed_resources  # noqa: E402
from storage import db  # noqa: E402

_PASSED = 0
_FAILED = 0

USER_ID = 555001
CHAT_ID = 555001


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  ok   {name}")
    else:
        _FAILED += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------
# Поддельная сессия: записывает вызовы вместо реальных запросов
# --------------------------------------------------------------------------

class RecordingSession(BaseSession):
    """Ничего не отправляет — складывает вызовы в список."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._message_id = 1000

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        self.calls.append((name, data))
        self._message_id += 1

        if name in ("SendMessage", "SendPhoto", "EditMessageText", "EditMessageCaption"):
            return Message(
                message_id=self._message_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=CHAT_ID, type="private"),
                text=data.get("text") or data.get("caption") or "",
            ).as_(bot)
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="Bot", username="testbot")
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    def clear(self) -> None:
        self.calls.clear()

    # -- удобные выборки ----------------------------------------------------

    @property
    def texts(self) -> list[str]:
        out = []
        for name, data in self.calls:
            value = data.get("text") or data.get("caption")
            if value:
                out.append(value)
        return out

    @property
    def all_text(self) -> str:
        return "\n".join(self.texts)

    def buttons(self) -> list[str]:
        """Надписи всех inline-кнопок из записанных вызовов."""
        labels = []
        for _, data in self.calls:
            markup = data.get("reply_markup") or {}
            for row in (markup.get("inline_keyboard") or []):
                for button in row:
                    labels.append(button.get("text", ""))
        return labels

    def callback_datas(self) -> list[str]:
        values = []
        for _, data in self.calls:
            markup = data.get("reply_markup") or {}
            for row in (markup.get("inline_keyboard") or []):
                for button in row:
                    if button.get("callback_data"):
                        values.append(button["callback_data"])
        return values

    def reply_keyboard(self) -> list[str]:
        for _, data in self.calls:
            markup = data.get("reply_markup") or {}
            for row in (markup.get("keyboard") or []):
                for button in row:
                    return_value = button.get("text")
                    if return_value:
                        return [
                            b.get("text", "")
                            for r in markup["keyboard"] for b in r
                        ]
        return []


session = RecordingSession()
tb.bot.session = session

_update_id = 0


def _next_update_id() -> int:
    global _update_id
    _update_id += 1
    return _update_id


def _user() -> User:
    return User(id=USER_ID, is_bot=False, first_name="Тест", username="tester",
                language_code="ru")


async def send_text(text: str) -> RecordingSession:
    """Имитирует сообщение от пользователя."""
    session.clear()
    update = Update(
        update_id=_next_update_id(),
        message=Message(
            message_id=_next_update_id(),
            date=datetime.now(timezone.utc),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=_user(),
            text=text,
        ),
    )
    await tb.dp.feed_update(tb.bot, update)
    return session


async def press(callback_data: str, *, has_photo: bool = False) -> RecordingSession:
    """Имитирует нажатие inline-кнопки."""
    session.clear()
    message = Message(
        message_id=_next_update_id(),
        date=datetime.now(timezone.utc),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=1, is_bot=True, first_name="Bot", username="testbot"),
        text="предыдущее сообщение бота",
    )
    update = Update(
        update_id=_next_update_id(),
        callback_query=CallbackQuery(
            id=str(_next_update_id()),
            from_user=_user(),
            chat_instance="test",
            message=message,
            data=callback_data,
        ),
    )
    await tb.dp.feed_update(tb.bot, update)
    return session


# --------------------------------------------------------------------------
# Проверка HTML-разметки и лимитов
# --------------------------------------------------------------------------

_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "tg-spoiler", "blockquote"}
_TAG_RE = re.compile(r"</?([a-zA-Z0-9-]+)[^>]*>")


def html_is_valid(text: str) -> tuple[bool, str]:
    """
    Telegram отклоняет ВСЁ сообщение при невалидном HTML, поэтому
    проверяем: только разрешённые теги и правильная вложенность.
    """
    stack: list[str] = []
    for match in _TAG_RE.finditer(text):
        tag = match.group(1).lower()
        if tag not in _ALLOWED_TAGS:
            return False, f"недопустимый тег <{tag}>"
        if match.group(0).startswith("</"):
            if not stack or stack[-1] != tag:
                return False, f"непарный закрывающий </{tag}>"
            stack.pop()
        elif not match.group(0).endswith("/>"):
            stack.append(tag)
    if stack:
        return False, f"незакрытый тег <{stack[-1]}>"
    return True, ""


def check_outgoing(label: str, recorded: RecordingSession) -> None:
    """Общая проверка всего, что бот собрался отправить."""
    for name, data in recorded.calls:
        text = data.get("text") or data.get("caption") or ""
        if not text:
            continue

        limit = 1024 if data.get("caption") else 4096
        if len(text) > limit:
            check(f"{label}: длина в пределах лимита", False,
                  f"{len(text)} > {limit}")
            return

        ok, reason = html_is_valid(text)
        if not ok:
            check(f"{label}: валидный HTML", False, f"{reason} в «{text[:80]}…»")
            return

    for value in recorded.callback_datas():
        if len(value.encode("utf-8")) > 64:
            check(f"{label}: callback_data ≤ 64 байт", False,
                  f"{len(value.encode())} байт: {value}")
            return

    check(f"{label}: разметка и лимиты в порядке", True)


# --------------------------------------------------------------------------
# Сценарии
# --------------------------------------------------------------------------

async def scenario_new_user() -> None:
    print("\nНовый пользователь")

    recorded = await send_text("/start")
    check("бот ответил на /start", bool(recorded.texts))
    check("приветствие упоминает разделы",
          "Что делать" in recorded.all_text or "памятки" in recorded.all_text)

    keyboard = recorded.reply_keyboard()
    check("показана постоянная клавиатура", len(keyboard) == 8, str(keyboard))
    check("есть кнопка помощника", tb.BTN_ASSISTANT in keyboard)
    check("есть кнопка ситуаций", tb.BTN_SITUATIONS in keyboard)
    check_outgoing("/start", recorded)

    # Пользователь должен попасть в базу аудитории.
    stats = db.count_audience(settings.db_path)
    check("пользователь учтён в аудитории", stats["total"] >= 1, str(stats))


async def scenario_situations() -> None:
    print("\nРаздел «Что делать, если…»")

    recorded = await send_text(tb.BTN_SITUATIONS)
    check("кнопка меню открыла раздел", "Что делать" in recorded.all_text)
    check("показаны ситуации", len(recorded.buttons()) >= 10)
    check_outgoing("список ситуаций", recorded)

    for situation in tb.SITUATIONS:
        recorded = await press(f"sit:show:{situation.key}")
        text = recorded.all_text
        ok = situation.title in text and bool(text)
        check(f"открывается: {situation.button}", ok)
        check_outgoing(f"памятка {situation.key}", recorded)

    # Ключевая проверка: в памятке о пропаже человека не должно быть
    # совета «подождите трое суток» — это опасный миф.
    recorded = await press("sit:show:missing")
    check("нет совета ждать трое суток",
          "ждать трое суток не нужно" in recorded.all_text.lower()
          or "не ждите трое суток" in recorded.all_text.lower())


async def scenario_phones() -> None:
    print("\nЭкстренные телефоны")

    recorded = await send_text(tb.BTN_PHONES)
    text = recorded.all_text
    check("есть 112", "112" in text)
    check("есть 102", "102" in text)
    check("есть телефон доверия МВД", "222-74-47" in text)
    check_outgoing("телефоны", recorded)


async def scenario_resources() -> None:
    print("\nСправочник источников")

    recorded = await send_text(tb.BTN_SOURCES)
    check("открылся справочник", "источник" in recorded.all_text.lower())
    check_outgoing("справочник", recorded)

    for key in ("tg", "max", "edu"):
        recorded = await press(f"res:cat:{key}")
        check(f"категория {key} открывается", bool(recorded.calls))
        check_outgoing(f"категория {key}", recorded)

    # Пагинация по 85 регионам MAX.
    recorded = await press("res:cat:max:5")
    check("пагинация работает", bool(recorded.calls))
    check_outgoing("страница 6", recorded)

    # Конкретный регион.
    recorded = await press("res:reg:max:0")
    check("регион открывается", bool(recorded.calls))
    check_outgoing("регион", recorded)

    # Несуществующий индекс не должен ронять хендлер.
    recorded = await press("res:reg:max:9999")
    check("некорректный индекс обработан", bool(recorded.calls))


async def scenario_region() -> None:
    print("\nМой регион")

    recorded = await send_text(tb.BTN_REGION)
    check("открылся выбор региона", "регион" in recorded.all_text.lower())
    check_outgoing("выбор региона", recorded)

    recorded = await press("myreg:set:0")
    check("регион выбирается", bool(recorded.calls))
    saved = db.get_audience_region(settings.db_path, USER_ID)
    check("регион сохранён в базе", saved is not None, str(saved))
    check_outgoing("карточка региона", recorded)

    recorded = await press("myreg:page:2")
    check("листание регионов работает", bool(recorded.calls))

    recorded = await press("myreg:clear")
    check("регион сбрасывается",
          db.get_audience_region(settings.db_path, USER_ID) is None)


async def scenario_news() -> None:
    print("\nХорошие новости")

    recorded = await send_text(tb.BTN_NEWS)
    check("раздел отвечает", bool(recorded.texts))
    check_outgoing("новости", recorded)


async def scenario_education_about() -> None:
    print("\nУчёба и о проекте")

    recorded = await send_text(tb.BTN_EDUCATION)
    check("раздел учёбы отвечает", "вуз" in recorded.all_text.lower())
    check_outgoing("учёба", recorded)

    recorded = await send_text(tb.BTN_ABOUT)
    check("раздел о проекте отвечает", "проект" in recorded.all_text.lower())
    check_outgoing("о проекте", recorded)


async def scenario_assistant_offline() -> None:
    print("\nПомощник без ключа AI")

    # AI_PROVIDER_API_KEY пуст: бот обязан вежливо перенаправить
    # на готовые памятки, а не молчать и не падать.
    recorded = await send_text(tb.BTN_ASSISTANT)
    check("помощник отвечает без ключа", bool(recorded.texts))
    check("предлагает памятки",
          "Что делать" in recorded.all_text or "памятк" in recorded.all_text.lower())
    check_outgoing("помощник офлайн", recorded)

    recorded = await send_text("У меня украли телефон, что делать?")
    check("свободный текст обработан", bool(recorded.texts))
    check_outgoing("свободный текст", recorded)

    # Приветствия не должны уходить модели: ответа по сути там нет,
    # а суточный лимит вопросов расходуется.
    for greeting in ("привет", "Спасибо!", "ок"):
        recorded = await send_text(greeting)
        check(f"«{greeting}» не уходит помощнику",
              "Здравствуйте" in recorded.all_text
              or "Выберите раздел" in recorded.all_text,
              recorded.all_text[:70])


async def scenario_menu_not_swallowed() -> None:
    print("\nКнопки меню не перехватываются помощником")

    # Пользователь вошёл в режим «жду вопрос», но нажал кнопку меню —
    # это переход в раздел, а не вопрос к ИИ.
    await press("ask:start")
    recorded = await send_text(tb.BTN_PHONES)
    check("кнопка меню в режиме вопроса открывает раздел",
          "112" in recorded.all_text, recorded.all_text[:100])

    await press("ask:start")
    recorded = await send_text(tb.BTN_SITUATIONS)
    check("кнопка ситуаций не съедена помощником",
          "Что делать" in recorded.all_text)


async def scenario_unknown_input() -> None:
    print("\nНеожиданный ввод")

    recorded = await send_text("/несуществующая_команда")
    check("неизвестная команда не роняет бота", True)

    recorded = await send_text("а")
    check("очень короткий текст обработан", bool(recorded.texts))

    recorded = await send_text("<b>инъекция</b><script>alert(1)</script>")
    check("HTML во вводе не ломает ответ", bool(recorded.texts))
    check_outgoing("ввод с HTML", recorded)

    recorded = await send_text("вопрос " * 300)
    check("очень длинный текст обработан", bool(recorded.texts))
    check_outgoing("длинный ввод", recorded)


async def scenario_no_access() -> None:
    print("\nСлужебные команды недоступны обычному пользователю")

    for command in ("/fetch", "/diag", "/stats", "/broadcast текст", "/audience"):
        recorded = await send_text(command)
        text = recorded.all_text
        denied = "нет доступа" in text.lower() or not text
        check(f"{command} закрыт для обычного пользователя", denied, text[:80])


async def main() -> int:
    db.init_db(settings.db_path)
    db.add_resources_bulk(settings.db_path, build_seed_resources())

    scenarios = (
        scenario_new_user, scenario_situations, scenario_phones,
        scenario_resources, scenario_region, scenario_news,
        scenario_education_about, scenario_assistant_offline,
        scenario_menu_not_swallowed, scenario_unknown_input,
        scenario_no_access,
    )

    for scenario in scenarios:
        try:
            await scenario()
        except Exception:
            global _FAILED
            _FAILED += 1
            print(f"\n  ИСКЛЮЧЕНИЕ в {scenario.__name__}:")
            import traceback
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"Пройдено: {_PASSED}   Провалено: {_FAILED}")
    print("=" * 60)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
