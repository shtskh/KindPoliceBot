"""
Веб-панель модерации. Читает/пишет ту же SQLite-базу, что и Telegram-бот
(storage/db.py) — модерировать можно из веб-интерфейса ИЛИ из Telegram,
результат одинаковый.

При нажатии «Опубликовать» панель сама отправляет пост через
TelegramDelivery (тем же TELEGRAM_BOT_TOKEN и PUBLISH_CHAT_ID, что и
бот) — единая точка публикации, чтобы пост, отправленный из двух разных
интерфейсов, не задвоился.

БЕЗОПАСНОСТЬ
------------
БЫЛ БАГ: в config.py были заведены PANEL_USERNAME/PANEL_PASSWORD, и в
документации панель описывалась как защищённая — но в коде проверка
отсутствовала полностью. Любой, кто знал адрес и порт, мог открыть
очередь и опубликовать что угодно в канал от имени проекта. Теперь
включён HTTP Basic Auth, а без заданных логина/пароля панель вообще
не отдаёт страницы (иначе «забыл настроить» снова означало бы
«открыто всем»).

ВАЖНО: Basic Auth передаёт логин/пароль в base64, не шифруя их. В
проде панель обязана стоять за HTTPS (nginx/Caddy как reverse proxy).

Запуск:
    uvicorn app:app --host 127.0.0.1 --port 8000
Открыть: http://localhost:8000
"""
from __future__ import annotations

import html
import secrets
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import settings, validate_settings
from delivery.telegram_delivery import TelegramDelivery
from logging_setup import setup_logging
from paths import resolve
from pipeline import image_pipeline
from storage import db
from storage.models import NewsStatus

logger = setup_logging(settings.log_level, settings.log_to_file)

app = FastAPI(title="Модерация — Хороший полицейский")
security = HTTPBasic()


@app.on_event("startup")
def on_startup() -> None:
    validate_settings()
    db.init_db(settings.db_path)
    if not settings.panel_username or not settings.panel_password:
        logger.error(
            "PANEL_USERNAME/PANEL_PASSWORD не заданы — панель модерации будет "
            "отвечать 503 на все запросы. Задайте их в .env."
        )


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """
    Проверка HTTP Basic Auth.

    secrets.compare_digest вместо обычного == — сравнение за постоянное
    время, чтобы по задержке ответа нельзя было подбирать пароль
    посимвольно.
    """
    if not settings.panel_username or not settings.panel_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Панель не настроена: задайте PANEL_USERNAME и PANEL_PASSWORD.",
        )

    correct_user = secrets.compare_digest(
        credentials.username, settings.panel_username
    )
    correct_password = secrets.compare_digest(
        credentials.password, settings.panel_password
    )

    if not (correct_user and correct_password):
        logger.warning("Неудачная попытка входа в панель: %s", credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ==========================================================================
# Оформление
# ==========================================================================

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Модерация — Хороший полицейский</title>
<style>
  :root {{
    --bg: #f4f6fb;
    --surface: #ffffff;
    --border: #e2e7f0;
    --text: #16233c;
    --muted: #667394;
    --accent: #0b1a38;
    --gold: #e8b23a;
    --green: #1a7f37;
    --red: #cf222e;
    --shadow: 0 2px 14px rgba(16, 32, 68, .07);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0e1421;
      --surface: #161f31;
      --border: #26324a;
      --text: #e8edf7;
      --muted: #97a5c2;
      --accent: #dce5f5;
      --shadow: 0 2px 14px rgba(0, 0, 0, .35);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    margin: 0; padding: 0 16px 64px; line-height: 1.55;
  }}
  .wrap {{ max-width: 780px; margin: 0 auto; }}
  header {{
    display: flex; align-items: center; gap: 14px;
    padding: 28px 0 20px; border-bottom: 3px solid var(--gold);
    margin-bottom: 28px;
  }}
  .shield {{
    width: 40px; height: 46px; flex: none;
    background: var(--gold);
    clip-path: polygon(50% 0, 100% 8%, 100% 60%, 50% 100%, 0 60%, 0 8%);
  }}
  h1 {{ font-size: 20px; margin: 0; letter-spacing: .2px; }}
  .sub {{ color: var(--muted); font-size: 14px; margin-top: 2px; }}
  .stats {{
    display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 24px;
  }}
  .stat {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 16px; box-shadow: var(--shadow);
  }}
  .stat b {{ display: block; font-size: 22px; line-height: 1.2; }}
  .stat span {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
                letter-spacing: .4px; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 0; margin-bottom: 20px;
    overflow: hidden; box-shadow: var(--shadow);
  }}
  .card-body {{ padding: 18px 20px 20px; }}
  .place {{
    display: inline-block; background: var(--gold); color: #0b1a38;
    font-weight: 600; font-size: 13px; padding: 4px 12px;
    border-radius: 20px; margin-bottom: 10px;
  }}
  .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; }}
  .text {{ white-space: pre-wrap; margin-bottom: 14px; font-size: 15.5px; }}
  .notes {{
    color: var(--muted); font-size: 13px; border-left: 3px solid var(--border);
    padding-left: 12px; margin-bottom: 16px;
  }}
  .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .actions form {{ margin: 0; }}
  button {{
    padding: 9px 18px; border-radius: 8px; border: none; cursor: pointer;
    font-size: 14px; font-weight: 600; font-family: inherit;
    transition: opacity .15s;
  }}
  button:hover {{ opacity: .85; }}
  .approve {{ background: var(--green); color: #fff; }}
  .reject {{ background: var(--red); color: #fff; }}
  .neutral {{ background: var(--border); color: var(--text); }}
  .empty {{
    text-align: center; color: var(--muted); padding: 60px 20px;
    background: var(--surface); border: 1px dashed var(--border);
    border-radius: 14px;
  }}
  /* Карточка-иллюстрация квадратная (1080x1080) и без ограничения
     занимала бы весь экран, отталкивая текст и кнопки за пределы
     видимой области — модератору пришлось бы прокручивать каждую
     новость, чтобы добраться до «Опубликовать». */
  img.photo {{
    width: 100%; max-height: 340px; object-fit: cover; object-position: top;
    display: block; background: var(--border); cursor: zoom-in;
  }}
  .photo-box {{ position: relative; }}
  .photo-box summary {{
    list-style: none; cursor: pointer;
  }}
  .photo-box summary::-webkit-details-marker {{ display: none; }}
  .photo-box[open] img.photo {{ max-height: none; cursor: zoom-out; }}
  .photo-hint {{
    position: absolute; right: 10px; bottom: 10px; pointer-events: none;
    background: rgba(11, 26, 56, .72); color: #fff; font-size: 12px;
    padding: 3px 9px; border-radius: 6px;
  }}
  .flash {{
    padding: 12px 16px; border-radius: 10px; margin-bottom: 20px;
    font-size: 14px; border: 1px solid;
  }}
  .flash.ok {{ background: rgba(26,127,55,.10); border-color: rgba(26,127,55,.35); }}
  .flash.err {{ background: rgba(207,34,46,.10); border-color: rgba(207,34,46,.35); }}
  footer {{ color: var(--muted); font-size: 12.5px; text-align: center;
            margin-top: 36px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="shield"></div>
    <div>
      <h1>Модерация новостей</h1>
      <div class="sub">Хороший полицейский · панель курсанта</div>
    </div>
  </header>
  {flash}
  <div class="stats">
    <div class="stat"><b>{pending}</b><span>на модерации</span></div>
    <div class="stat"><b>{published}</b><span>опубликовано</span></div>
    <div class="stat"><b>{rejected}</b><span>отклонено</span></div>
  </div>
  {items}
  <footer>Обновлено {now}</footer>
</div>
</body>
</html>"""

ITEM_TEMPLATE = """
<div class="card">
  {image_html}
  <div class="card-body">
    {place_html}
    <div class="meta">{source_name}{date_html}</div>
    <div class="text">{text}</div>
    {notes_html}
    <div class="actions">
      <form method="post" action="/approve/{id}">
        <button class="approve" type="submit">✅ Опубликовать</button>
      </form>
      <form method="post" action="/reject/{id}">
        <button class="reject" type="submit">❌ Отклонить</button>
      </form>
      <form method="get" action="{source_url}" target="_blank">
        <button class="neutral" type="submit">🔗 Источник</button>
      </form>
    </div>
  </div>
</div>
"""

_MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _format_date(value: datetime | None) -> str:
    if not value:
        return ""
    try:
        return f"{value.day} {_MONTHS_RU[value.month - 1]} {value.year}"
    except (IndexError, ValueError, AttributeError):
        return ""


def _render_page(flash: str = "") -> str:
    items = db.get_by_status(settings.db_path, NewsStatus.PENDING_MODERATION)
    counts = db.count_by_status(settings.db_path)

    if not items:
        rendered = (
            '<div class="empty">✨ Очередь пуста — всё обработано.<br>'
            'Запустите сбор командой /fetch в Telegram-боте.</div>'
        )
    else:
        rendered = ""
        for item in items:
            # <details> вместо картинки во всю высоту: по умолчанию видна
            # верхняя часть, по клику разворачивается целиком. Работает
            # без единой строчки JavaScript.
            image_html = (
                f'<details class="photo-box"><summary>'
                f'<img class="photo" src="/image/{item.id}" alt="">'
                f'<span class="photo-hint">нажмите, чтобы раскрыть</span>'
                f'</summary></details>'
                if item.image_path and image_pipeline.image_exists(item.image_path)
                else ""
            )

            place_parts = [p for p in (item.city, item.region) if p]
            place_html = (
                f'<div class="place">📍 {html.escape(" · ".join(place_parts))}</div>'
                if place_parts else ""
            )

            date_label = _format_date(item.published_at)
            date_html = f" · {date_label}" if date_label else ""

            notes_html = (
                f'<div class="notes">{html.escape(item.verification_notes)}</div>'
                if item.verification_notes else ""
            )

            # item.rewritten_text уже безопасный HTML (санитизирован в
            # rewrite.py — только <b>/<i>), поэтому НЕ экранируем повторно:
            # иначе теги стали бы видимым текстом. Сырой title, наоборот,
            # экранируем — он приходит из внешнего источника как есть.
            text = item.rewritten_text or html.escape(item.title or "")

            rendered += ITEM_TEMPLATE.format(
                id=item.id,
                source_name=html.escape(item.source_name or "источник неизвестен"),
                date_html=date_html,
                place_html=place_html,
                notes_html=notes_html,
                text=text,
                image_html=image_html,
                source_url=html.escape(item.source_url or "#", quote=True),
            )

    return PAGE_TEMPLATE.format(
        flash=flash,
        items=rendered,
        pending=len(items),
        published=counts.get(NewsStatus.PUBLISHED.value, 0),
        rejected=counts.get(NewsStatus.REJECTED.value, 0),
        now=datetime.now().strftime("%d.%m.%Y %H:%M"),
    )


# ==========================================================================
# Маршруты
# ==========================================================================

@app.get("/", response_class=HTMLResponse)
def moderation_queue(_: str = Depends(require_auth)):
    return _render_page()


@app.get("/image/{item_id}")
def get_image(item_id: str, _: str = Depends(require_auth)):
    item = db.get_by_id(settings.db_path, item_id)
    if not item or not item.image_path:
        raise HTTPException(status_code=404, detail="Изображение не найдено")

    path = resolve(item.image_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл изображения отсутствует")

    return FileResponse(path)


@app.post("/approve/{item_id}")
def approve(item_id: str, _: str = Depends(require_auth)):
    from bot import build_post_text  # локальный импорт: тот же формат поста

    item = db.get_by_id(settings.db_path, item_id)
    if not item:
        return RedirectResponse(url="/", status_code=303)

    if not settings.publish_chat_id:
        # Нет канала публикации — фиксируем одобрение, но ничего не
        # отправляем, чтобы не падать молча.
        db.claim_for_moderation(settings.db_path, item_id, NewsStatus.APPROVED)
        return RedirectResponse(url="/", status_code=303)

    # Тот же атомарный «захват», что и в боте: без него модератор в
    # Telegram и модератор в панели могли опубликовать один пост дважды.
    if not db.claim_for_moderation(settings.db_path, item_id, NewsStatus.APPROVED):
        logger.info("Новость %s уже обработана — публикация пропущена.", item_id)
        return RedirectResponse(url="/", status_code=303)

    delivery = TelegramDelivery(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.publish_chat_id,
    )
    result = delivery.send_post_detailed(build_post_text(item), item.image_path)

    if result.ok:
        db.update_status(settings.db_path, item_id, NewsStatus.PUBLISHED)
        db.set_published_message_id(settings.db_path, item_id, result.message_id)
        logger.info("Новость %s опубликована через веб-панель", item_id)
    else:
        # Публикация не удалась — возвращаем в очередь, иначе новость
        # застряла бы в статусе approved и пропала из панели.
        db.update_status(settings.db_path, item_id, NewsStatus.PENDING_MODERATION)
        logger.error("Публикация %s из панели не удалась: %s", item_id, result.error)

    return RedirectResponse(url="/", status_code=303)


@app.post("/reject/{item_id}")
def reject(item_id: str, _: str = Depends(require_auth)):
    db.claim_for_moderation(settings.db_path, item_id, NewsStatus.REJECTED)
    return RedirectResponse(url="/", status_code=303)


@app.get("/healthz")
def healthz():
    """Проверка живости для мониторинга — без авторизации, без данных."""
    return {"status": "ok"}
