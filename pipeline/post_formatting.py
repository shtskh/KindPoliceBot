"""
Обязательная подпись, которая добавляется к КАЖДОМУ посту — ссылка на
канал и на самого бота. Используется в orchestrator.py сразу после
рерайта, поэтому дальше по пайплайну (модерация, публикация — и через
бота, и через веб-панель) везде фигурирует уже готовый текст с подписью:
одна точка добавления вместо трёх копий одной и той же логики.

Формат — HTML-ссылки (<a href="...">...</a>), совместимые с
parse_mode=HTML. Текст, к которому добавляется подпись, ожидается уже
безопасным (см. rewrite.py: sanitize_rewritten_html).
"""
from config import settings

FOOTER_MARKER = "\u200b"  # zero-width space — метка "подпись уже добавлена"


def build_footer() -> str:
    links = []
    if settings.channel_username:
        links.append(
            f'<a href="https://t.me/{settings.channel_username}">Хороший полицейский</a>'
        )
    if settings.bot_username:
        links.append(f'<a href="https://t.me/{settings.bot_username}">Бот</a>')

    if not links:
        return ""

    return f"\n\n{FOOTER_MARKER}📌 " + " | ".join(links)


def append_footer_if_missing(text: str) -> str:
    """
    Идемпотентно: если подпись уже есть (по метке FOOTER_MARKER),
    не дублирует её. Нужно на случай повторной обработки одной и той
    же новости (например, при ретрае пайплайна).
    """
    text = (text or "").strip()
    if not text:
        return text
    if FOOTER_MARKER in text:
        return text
    footer = build_footer()
    return text + footer if footer else text
