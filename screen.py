"""
Витрина для экрана: опубликованные посты крутятся по кругу.

ЗАЧЕМ
-----
Канал читают с телефона, но те же новости хорошо смотрятся на экране
в холле отдела, в учебном классе, на стенде в вузе МВД или на
конференции. Для этого нужен не канал, а страница, которая сама
листает посты и не требует человека рядом.

УСТРОЙСТВО
----------
Три публичных маршрута:

  /screen              — сама страница (HTML, ничего не грузит извне);
  /screen/data         — список постов в JSON;
  /screen/media/{id}   — картинка или видео поста.

Почему без пароля: на экране показывается только то, что уже вышло в
канал, то есть общедоступное. А вводить логин на телевизоре без
клавиатуры невозможно. Очередь модерации остаётся закрытой.

Почему страница самодостаточна: ни одного обращения к внешним CDN за
шрифтами и библиотеками. Экран может стоять в сети без интернета,
рядом с сервером, и должен работать там точно так же.

ДВА ФОРМАТА
-----------
Раскладка выбирается не по устройству, а по ориентации окна —
`@media (orientation: portrait)`. Телевизор и монитор дают 16:9,
телефон и вертикальная панель — 9:16. Это надёжнее, чем угадывать
устройство по строке браузера: вертикальные экраны в холлах сейчас
обычное дело, а их браузер ничем не отличается от настольного.

ЭКРАН РАБОТАЕТ СУТКАМИ
----------------------
Отсюда три решения, которых не нужно обычной странице:

  * список постов обновляется сам, без перезагрузки вкладки;
  * картинка медленно наезжает (эффект Кена Бёрнса) — неподвижная
    яркая область на матрице за недели даёт выгорание;
  * следующая картинка загружается заранее, иначе на переходе будет
    пустое место.
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from config import settings
from logging_setup import get_logger
from paths import resolve
from storage import db

logger = get_logger("screen")

router = APIRouter()

_MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

# Форматы, которые браузер играет как видео. Остальное отдаём картинкой.
_VIDEO_SUFFIXES = (".mp4", ".webm", ".mov")


def _format_date(value: datetime | None) -> str:
    if not value:
        return ""
    try:
        return f"{value.day} {_MONTHS_RU[value.month - 1]} {value.year}"
    except (IndexError, ValueError, AttributeError):
        return ""


def _place(item) -> str:
    """«Тверская область · деревня Квакшино» — то, что делает новость
    своей для зрителя. Город без региона бесполезен, регион без города
    всё равно информативен."""
    parts = [p for p in (item.region, item.city) if p]
    return " · ".join(parts)


def _post_text(item) -> str:
    """Для экрана берём переписанный текст: исходный — это простыня
    канцелярита, которую со стены не прочитать."""
    return (item.rewritten_text or item.title or "").strip()


def _media_kind(item) -> str | None:
    if item.video_path:
        return "video"
    if item.image_path:
        return "image"
    return None


@router.get("/screen/data")
def screen_data(
    limit: int | None = Query(default=None, ge=1, le=100),
    region: str | None = Query(default=None, max_length=120),
) -> JSONResponse:
    """
    Опубликованные посты для витрины.

    `region` позволяет повесить в здании УМВД экран со своим регионом,
    а в вузе — с общероссийской лентой, не поднимая второй сервер.
    """
    if not settings.screen_enabled:
        raise HTTPException(status_code=404, detail="Витрина отключена")

    items = db.recent_published(
        settings.db_path,
        limit=limit or settings.screen_items,
        region=region or None,
    )

    posts = []
    for item in items:
        text = _post_text(item)
        if not text:
            # Пост без текста на экране — пустой слайд.
            continue
        posts.append({
            "id": item.id,
            "text": text,
            "place": _place(item),
            "date": _format_date(item.published_at),
            "source": item.source_name or "",
            "media": _media_kind(item),
            # Пометка выводится элементом страницы, а не только
            # впечатанной в сам файл: кадр на экране обрезается по
            # пропорциям (object-fit: cover), и впечатанная надпись
            # из правого нижнего угла может уехать за край.
            "generated": item.image_source == "generated",
            "media_url": f"/screen/media/{item.id}" if _media_kind(item) else None,
        })

    return JSONResponse({
        "channel": settings.channel_title,
        "interval": settings.screen_interval_seconds,
        "refresh": settings.screen_refresh_seconds,
        "posts": posts,
    })


@router.get("/screen/media/{item_id}")
def screen_media(item_id: str):
    """
    Картинка или видео поста.

    Отдаём только для опубликованных: иначе по прямой ссылке можно было
    бы вытащить снимок из новости, которую модератор ещё не смотрел или
    уже отклонил.
    """
    if not settings.screen_enabled:
        raise HTTPException(status_code=404, detail="Витрина отключена")

    item = db.get_by_id(settings.db_path, item_id)
    if not item or item.status.value != "published":
        raise HTTPException(status_code=404, detail="Пост не найден")

    stored = item.video_path or item.image_path
    if not stored:
        raise HTTPException(status_code=404, detail="У поста нет медиа")

    path = resolve(stored)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл отсутствует")

    # Кэш на сутки: файл по этому адресу уже не изменится, а экран
    # возвращается к тем же постам каждый круг.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/screen", response_class=HTMLResponse)
def screen_page(
    interval: int | None = Query(default=None, ge=5, le=3600),
    limit: int | None = Query(default=None, ge=1, le=100),
    region: str | None = Query(default=None, max_length=120),
) -> HTMLResponse:
    """
    Страница витрины.

    Параметры в адресе позволяют настроить конкретный экран, не трогая
    сервер: /screen?region=Тверская область&interval=120
    """
    if not settings.screen_enabled:
        raise HTTPException(status_code=404, detail="Витрина отключена")

    config = {
        "dataUrl": "/screen/data",
        "interval": interval or settings.screen_interval_seconds,
        "limit": limit,
        "region": region,
        "refresh": settings.screen_refresh_seconds,
        "title": settings.channel_title,
    }
    return HTMLResponse(PAGE_TEMPLATE.replace(
        "__CONFIG__", json.dumps(config, ensure_ascii=False)
    ))


# --------------------------------------------------------------------------
# Страница
# --------------------------------------------------------------------------
#
# Шрифт — системный: на витрине нельзя зависеть от внешней загрузки.
# Размеры заданы в vmin/vh, поэтому один и тот же макет читается и на
# 32-дюймовом мониторе, и на вертикальной панели, и на телефоне.

PAGE_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Добрые новости о полиции</title>
<style>
  :root {
    --bg: #0b1020;
    --panel: rgba(10, 16, 34, 0.82);
    --accent: #4f7df0;
    --text: #f3f5fb;
    --muted: #9aa6c4;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    overflow: hidden;
  }
  /* Курсор на витрине не нужен: мышь к экрану обычно не подключена,
     а если подключена — стрелка посреди кадра выглядит неопрятно. */
  body { cursor: none; }

  #stage { position: relative; width: 100vw; height: 100vh; }

  .slide {
    position: absolute;
    inset: 0;
    display: grid;
    opacity: 0;
    transition: opacity .7s ease;
  }
  .slide.visible { opacity: 1; }

  /* Горизонтальный экран (16:9): снимок слева, текст справа. */
  .slide { grid-template-columns: 58fr 42fr; grid-template-rows: 1fr; }

  /* Вертикальный экран (9:16): снимок сверху, текст под ним. */
  @media (orientation: portrait) {
    .slide { grid-template-columns: 1fr; grid-template-rows: 54fr 46fr; }
  }

  .media { position: relative; overflow: hidden; background: #060912; }
  .media img, .media video {
    width: 100%; height: 100%;
    object-fit: cover;
    display: block;
    /* Медленный наезд: неподвижная картинка часами подряд выжигает
       матрицу, а движение к тому же оживляет статичный слайд. */
    animation: drift 40s ease-in-out infinite alternate;
  }
  @keyframes drift {
    from { transform: scale(1.00) translate3d(0, 0, 0); }
    to   { transform: scale(1.08) translate3d(0, -1.5%, 0); }
  }
  /* Если в системе выключены анимации — уважаем настройку. */
  @media (prefers-reduced-motion: reduce) {
    .media img, .media video { animation: none; }
    .slide { transition: none; }
  }

  .body {
    background: var(--panel);
    padding: 5vmin 5vmin 4vmin;
    display: flex;
    flex-direction: column;
    gap: 2.4vmin;
    justify-content: center;
  }

  .place {
    color: var(--accent);
    font-size: 2.3vmin;
    font-weight: 700;
    letter-spacing: .14em;
    text-transform: uppercase;
  }
  .text {
    font-size: 4.1vmin;
    line-height: 1.34;
    font-weight: 600;
    /* Длинный текст лучше обрезать, чем сломать макет: на экране
       всё равно читают первые фразы. */
    overflow: hidden;
  }
  .meta {
    margin-top: auto;
    color: var(--muted);
    font-size: 1.9vmin;
    display: flex;
    gap: 1.6vmin;
    flex-wrap: wrap;
    align-items: center;
  }
  .meta .dot { opacity: .45; }

  /* Шапка и прогресс поверх кадра */
  .brand {
    position: absolute;
    top: 3.2vmin; left: 3.4vmin;
    display: flex; align-items: center; gap: 1.4vmin;
    padding: 1.1vmin 2vmin;
    background: rgba(8, 12, 26, .62);
    border-radius: 999px;
    font-size: 1.9vmin;
    font-weight: 700;
    letter-spacing: .04em;
    backdrop-filter: blur(6px);
  }
  .brand .mark {
    width: 1.5vmin; height: 1.5vmin;
    border-radius: 50%;
    background: var(--accent);
  }
  .clock {
    position: absolute;
    top: 3.2vmin; right: 3.4vmin;
    padding: 1.1vmin 2vmin;
    background: rgba(8, 12, 26, .62);
    border-radius: 999px;
    font-size: 1.9vmin;
    font-variant-numeric: tabular-nums;
    backdrop-filter: blur(6px);
  }
  .progress {
    position: absolute;
    left: 0; bottom: 0;
    height: .5vmin;
    width: 0%;
    background: var(--accent);
    box-shadow: 0 0 2vmin rgba(79, 125, 240, .7);
  }
  .counter {
    position: absolute;
    bottom: 2.4vmin; right: 3.4vmin;
    font-size: 1.7vmin;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }

  .notice {
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    text-align: center;
    padding: 8vmin;
    font-size: 3vmin;
    color: var(--muted);
    line-height: 1.5;
  }
  .hidden { display: none; }

  /* --- Вертикальный экран: размеры и выравнивание --- */
  @media (orientation: portrait) {
    /* Кегль крупнее: vmin считается от узкой стороны, и шрифт,
       нормальный для монитора, на вертикальной панели мелковат. */
    .place { font-size: 3.1vmin; }
    .text  { font-size: 5.6vmin; }
    .meta  { font-size: 2.6vmin; }
    /* Текст и подпись — одной группой по центру панели, иначе под
       коротким постом остаётся половина пустого экрана. */
    .body { justify-content: center; }
    .meta { margin-top: 3vmin; }
    .brand, .clock { font-size: 2.4vmin; }
    .counter { font-size: 2.2vmin; }
  }
</style>
</head>
<body>
  <div id="stage">
    <div id="notice" class="notice">Загружаем новости…</div>
  </div>
  <div class="brand"><span class="mark"></span><span id="brand-title"></span></div>
  <div class="clock" id="clock"></div>
  <div class="counter" id="counter"></div>
  <div class="progress" id="progress"></div>

<script>
const CONFIG = __CONFIG__;

const stage = document.getElementById('stage');
const notice = document.getElementById('notice');
const progress = document.getElementById('progress');
const counter = document.getElementById('counter');
const clock = document.getElementById('clock');
document.getElementById('brand-title').textContent = CONFIG.title;

let posts = [];
let index = 0;
let slideStarted = 0;
let paused = false;
let timer = null;

function esc(value) {
  const node = document.createElement('div');
  node.textContent = value == null ? '' : String(value);
  return node.innerHTML;
}

function dataUrl() {
  const params = new URLSearchParams();
  if (CONFIG.limit) params.set('limit', CONFIG.limit);
  if (CONFIG.region) params.set('region', CONFIG.region);
  const query = params.toString();
  return CONFIG.dataUrl + (query ? '?' + query : '');
}

async function loadPosts(initial) {
  try {
    const response = await fetch(dataUrl(), {cache: 'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    posts = data.posts || [];
    if (data.interval) CONFIG.interval = data.interval;
    if (posts.length === 0) {
      notice.textContent = 'Пока нет опубликованных новостей.';
      notice.classList.remove('hidden');
      stage.querySelectorAll('.slide').forEach(node => node.remove());
      return;
    }
    notice.classList.add('hidden');
    if (index >= posts.length) index = 0;
    if (initial) show(0);
  } catch (error) {
    // Сеть моргнула или сервер перезапускается — показываем то, что
    // уже загружено, и пробуем снова на следующем круге. Пустой экран
    // в холле хуже, чем слегка несвежая новость.
    if (posts.length === 0) {
      notice.textContent = 'Нет связи с сервером. Пробуем ещё раз…';
      notice.classList.remove('hidden');
    }
  }
}

function buildSlide(post) {
  const slide = document.createElement('div');
  slide.className = 'slide';

  let media = '<div class="media"></div>';
  if (post.media === 'video') {
    media = '<div class="media"><video src="' + esc(post.media_url) +
            '" autoplay muted loop playsinline></video></div>';
  } else if (post.media === 'image') {
    media = '<div class="media"><img src="' + esc(post.media_url) +
            '" alt=""></div>';
  }

  const place = post.place
    ? '<div class="place">' + esc(post.place) + '</div>' : '';
  const parts = [post.date, post.source];
  if (post.generated) parts.push('иллюстрация сгенерирована ИИ');
  const meta = parts.filter(Boolean)
    .map(esc).join(' <span class="dot">•</span> ');

  slide.innerHTML = media +
    '<div class="body">' + place +
      '<div class="text">' + esc(post.text) + '</div>' +
      '<div class="meta">' + meta + '</div>' +
    '</div>';
  return slide;
}

function preload(post) {
  if (post && post.media === 'image' && post.media_url) {
    const image = new Image();
    image.src = post.media_url;
  }
}

function show(next) {
  if (posts.length === 0) return;
  index = (next + posts.length) % posts.length;

  const slide = buildSlide(posts[index]);
  stage.appendChild(slide);
  // Кадр добавляем прозрачным и зажигаем на следующем кадре отрисовки,
  // иначе переход не успевает начаться и картинка появляется рывком.
  requestAnimationFrame(() => slide.classList.add('visible'));

  const previous = stage.querySelectorAll('.slide');
  if (previous.length > 1) {
    const old = previous[0];
    old.classList.remove('visible');
    setTimeout(() => old.remove(), 800);
  }

  counter.textContent = (index + 1) + ' / ' + posts.length;
  slideStarted = Date.now();
  preload(posts[(index + 1) % posts.length]);
}

function tick() {
  if (paused || posts.length === 0) return;
  const elapsed = Date.now() - slideStarted;
  const total = CONFIG.interval * 1000;
  progress.style.width = Math.min(100, (elapsed / total) * 100) + '%';
  if (elapsed >= total) show(index + 1);
}

function tickClock() {
  const now = new Date();
  clock.textContent = now.toLocaleTimeString('ru-RU',
    {hour: '2-digit', minute: '2-digit'});
}

// Управление с клавиатуры: пригодится на защите, на выставке и при
// настройке экрана. Пульты телевизоров шлют те же коды стрелок.
document.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowRight') show(index + 1);
  if (event.key === 'ArrowLeft') show(index - 1);
  if (event.key === ' ') { paused = !paused; slideStarted = Date.now(); }
  if (event.key.toLowerCase() === 'f') {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen();
  }
});

loadPosts(true);
timer = setInterval(tick, 100);
setInterval(tickClock, 1000);
tickClock();
setInterval(() => loadPosts(false), CONFIG.refresh * 1000);
</script>
</body>
</html>
"""
