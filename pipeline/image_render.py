"""
Локальный рендер красивой карточки-иллюстрации для поста.

ЗАЧЕМ ЭТО НУЖНО
---------------
Проверка боевого AI Provider (aiprovider.duckdns.org) показала: у него
НЕТ эндпоинта генерации изображений — POST /v1/images/generations
отвечает 404 "Endpoint not found" на любой модели, и в /v1/models нет
ни одной image-модели (только текстовые). Именно поэтому в базе за всё
время нет ни одной записи с image_source='generated': удалённая
генерация не могла сработать в принципе.

Чтобы у КАЖДОГО поста гарантированно была картинка, здесь рисуется
фирменная карточка средствами Pillow — локально, без сети, без ключей
и без шанса упасть по вине чужого API. Дополнительный плюс: карточка
честная — это оформленная цитата новости, а не сгенерированное
«фотореалистичное» изображение сотрудника полиции, которого не
существовало (для ведомственной тематики это принципиально важно:
выдавать AI-картинку за фотографию с места события нельзя).

Если Pillow не установлен, модуль сообщает об этом через
CARD_RENDER_AVAILABLE, а пайплайн просто останется без картинки —
падать из-за оформления нельзя.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from logging_setup import get_logger
from paths import IMAGES_DIR, ensure_dirs

logger = get_logger("card")

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    CARD_RENDER_AVAILABLE = True
except ImportError:  # pragma: no cover
    CARD_RENDER_AVAILABLE = False
    logger.warning(
        "Pillow не установлен — карточки-иллюстрации рисоваться не будут. "
        "Установить: pip install Pillow"
    )

CARD_WIDTH = 1080
CARD_HEIGHT = 1080

# Палитра: тёмно-синий «полицейский» градиент. Держим все цвета в одном
# месте, чтобы перекрасить оформление можно было одной правкой.
COLOR_BG_TOP = (11, 26, 56)        # глубокий синий
COLOR_BG_BOTTOM = (23, 55, 105)    # синий посветлее
COLOR_ACCENT = (232, 178, 58)      # золотой — кант, акценты
COLOR_TEXT = (255, 255, 255)
COLOR_TEXT_MUTED = (176, 197, 228)
COLOR_BADGE_BG = (232, 178, 58)
COLOR_BADGE_TEXT = (11, 26, 56)

MARGIN = 88

# Кандидаты шрифтов: сначала кириллические системные Windows/Linux,
# в конце — дефолтный битмап Pillow (некрасивый, но не даёт упасть).
_FONT_CANDIDATES_BOLD = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

_font_cache: dict[tuple[str, int], "ImageFont.FreeTypeFont"] = {}


def _load_font(bold: bool, size: int):
    """Первый доступный шрифт из списка; результат кешируется —
    ImageFont.truetype() читает файл с диска на каждый вызов."""
    key = ("bold" if bold else "regular", size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached

    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR
    for candidate in candidates:
        try:
            font = ImageFont.truetype(candidate, size)
            _font_cache[key] = font
            return font
        except (OSError, ValueError):
            continue

    logger.warning("Не найден TTF-шрифт с кириллицей, использую встроенный.")
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _vertical_gradient(width: int, height: int, top, bottom) -> "Image.Image":
    """Плавный вертикальный градиент — рисуем узкой полоской и растягиваем,
    так в разы быстрее, чем построчно по всей ширине."""
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        pixels[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * ratio),
            int(top[1] + (bottom[1] - top[1]) * ratio),
            int(top[2] + (bottom[2] - top[2]) * ratio),
        )
    return strip.resize((width, height), Image.BILINEAR)


def _add_glow(image: "Image.Image", center, radius: int, color, opacity: int):
    """
    Мягкое световое пятно — оживляет плоский градиент.

    Реализация: сплошная заливка нужного цвета накладывается на холст
    через размытую радиальную маску. Сила свечения задаётся яркостью
    маски (opacity 0..255), поэтому по краям пятно плавно уходит в ноль.
    """
    overlay = Image.new("RGB", image.size, color)

    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse(
        [center[0] - radius, center[1] - radius,
         center[0] + radius, center[1] + radius],
        fill=max(0, min(255, opacity)),
    )
    mask = mask.filter(ImageFilter.GaussianBlur(radius // 2))

    return Image.composite(overlay, image, mask)


def _draw_shield(draw, cx: int, cy: int, size: int, color, width: int = 5):
    """
    Контур щита — узнаваемый ведомственный символ, но НЕ герб и не
    официальная эмблема МВД: воспроизводить государственную символику в
    оформлении неофициального канала нельзя, поэтому рисуем нейтральную
    геометрическую форму.
    """
    half = size / 2
    top = cy - half
    bottom = cy + half
    points = [
        (cx - half, top + size * 0.06),
        (cx, top),
        (cx + half, top + size * 0.06),
        (cx + half, cy + size * 0.10),
        (cx, bottom),
        (cx - half, cy + size * 0.10),
    ]
    draw.polygon(points, outline=color, width=width)


def _wrap_text(text: str, font, max_width: int, draw) -> list[str]:
    """
    Перенос по словам с учётом реальной ширины глифов (textwrap считает
    символы, а в пропорциональном шрифте «ш» шире «і», и строка уезжает
    за поле). Слишком длинные слова (ссылки, длинные топонимы) режем
    принудительно, иначе они вылезут за границу карточки.
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)

    # Принудительный разрыв слов, которые сами по себе шире поля.
    result: list[str] = []
    for line in lines:
        while draw.textlength(line, font=font) > max_width and len(line) > 1:
            cut = len(line)
            while cut > 1 and draw.textlength(line[:cut], font=font) > max_width:
                cut -= 1
            result.append(line[:cut])
            line = line[cut:]
        result.append(line)
    return result


def _fit_text(
    text: str, draw, max_width: int, max_height: int,
    sizes: tuple[int, ...] = (66, 60, 54, 48, 44, 40, 36, 32),
):
    """
    Подбирает наибольший кегль, при котором текст помещается в отведённый
    блок. Так короткая новость смотрится крупно и «плакатно», а длинная
    просто набирается мельче — вместо того чтобы обрезаться на полуслове.
    """
    for size in sizes:
        font = _load_font(bold=True, size=size)
        line_height = int(size * 1.32)
        lines = _wrap_text(text, font, max_width, draw)
        if len(lines) * line_height <= max_height:
            return font, lines, line_height

    # Даже минимальным кеглем не влезло — обрезаем по числу строк.
    font = _load_font(bold=True, size=sizes[-1])
    line_height = int(sizes[-1] * 1.32)
    lines = _wrap_text(text, font, max_width, draw)
    max_lines = max(1, max_height // line_height)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.;:—-") + "…"
    return font, lines, line_height


# Официальные названия регионов длинные и в плашку не помещаются.
# Короткая форма читается лучше и остаётся однозначной.
_REGION_SHORT_FORMS = {
    "ханты-мансийский автономный округ — югра": "Югра",
    "ханты-мансийский автономный округ - югра": "Югра",
    "ямало-ненецкий автономный округ": "ЯНАО",
    "ненецкий автономный округ": "НАО",
    "чукотский автономный округ": "Чукотка",
    "еврейская автономная область": "ЕАО",
    "кабардино-балкарская республика": "Кабардино-Балкария",
    "карачаево-черкесская республика": "Карачаево-Черкесия",
    "республика северная осетия — алания": "Северная Осетия",
    "республика северная осетия - алания": "Северная Осетия",
    "республика саха (якутия)": "Якутия",
    "удмуртская республика": "Удмуртия",
    "чувашская республика": "Чувашия",
    "кемеровская область — кузбасс": "Кузбасс",
    "кемеровская область - кузбасс": "Кузбасс",
}


def _shorten_region(region: str) -> str:
    """«Республика Татарстан» -> «Татарстан» и т.п. — короче и без канцелярита."""
    normalized = region.strip()
    short = _REGION_SHORT_FORMS.get(normalized.lower())
    if short:
        return short

    lowered = normalized.lower()
    for prefix in ("республика ", "г. ", "город "):
        if lowered.startswith(prefix):
            return normalized[len(prefix):].strip()
    return normalized


def _format_place(region: str | None, city: str | None) -> str:
    """
    Строка места для плашки: «Город · Регион», без дублирования
    (для Москвы/Санкт-Петербурга город и регион совпадают) и с короткими
    формами длинных названий регионов.
    """
    parts: list[str] = []
    if city:
        parts.append(city.strip())
    if region:
        parts.append(_shorten_region(region))

    unique: list[str] = []
    for part in parts:
        if not part:
            continue
        # Пропускаем регион, если он повторяет город («Москва» / «Москва»)
        # или уже входит в него как подстрока.
        if any(part.lower() in u.lower() or u.lower() in part.lower() for u in unique):
            continue
        unique.append(part)

    return " · ".join(unique)


def _draw_map_pin(draw, x: int, y: int, size: int, color):
    """
    Значок геометки, нарисованный вручную.

    Эмодзи 📍 использовать нельзя: в системных TTF (Segoe UI, Arial,
    DejaVu) нет цветных эмодзи-глифов, и Pillow рисует вместо символа
    пустой прямоугольник-«тофу». Простая векторная капля выглядит
    аккуратно и работает на любой машине.
    """
    radius = size * 0.34
    cx = x + size / 2
    cy = y + radius + size * 0.04
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color)
    draw.polygon(
        [(cx - radius * 0.72, cy + radius * 0.52),
         (cx + radius * 0.72, cy + radius * 0.52),
         (cx, y + size)],
        fill=color,
    )
    hole = radius * 0.36
    draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill=COLOR_BADGE_BG)


_MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _format_date(published_at: datetime | None) -> str:
    if not published_at:
        return ""
    try:
        return f"{published_at.day} {_MONTHS_RU[published_at.month - 1]} {published_at.year}"
    except (IndexError, AttributeError, ValueError):
        return ""


def render_news_card(
    text: str,
    *,
    region: str | None = None,
    city: str | None = None,
    published_at: datetime | None = None,
    channel_title: str = "Хороший полицейский",
    source_name: str | None = None,
    output_path: str | Path | None = None,
) -> str | None:
    """
    Рисует карточку 1080×1080 и возвращает путь к файлу (или None, если
    Pillow недоступен либо рендер не удался — вызывающий код должен уметь
    жить без картинки, оформление не повод терять новость).
    """
    if not CARD_RENDER_AVAILABLE:
        return None

    text = (text or "").strip()
    if not text:
        logger.warning("Нечего рисовать: пустой текст новости.")
        return None

    try:
        ensure_dirs()

        canvas = _vertical_gradient(
            CARD_WIDTH, CARD_HEIGHT, COLOR_BG_TOP, COLOR_BG_BOTTOM
        )
        canvas = _add_glow(
            canvas, (CARD_WIDTH - 160, 180), 520, (58, 110, 190), 120
        )
        canvas = _add_glow(
            canvas, (120, CARD_HEIGHT - 120), 420, (16, 40, 82), 110
        )
        draw = ImageDraw.Draw(canvas)

        # --- Декор: крупный полупрозрачный щит в правом нижнем углу ---
        shield_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        shield_draw = ImageDraw.Draw(shield_layer)
        _draw_shield(
            shield_draw, CARD_WIDTH - 150, CARD_HEIGHT - 230, 560,
            (255, 255, 255, 22), width=10,
        )
        canvas = Image.alpha_composite(canvas.convert("RGBA"), shield_layer).convert("RGB")
        draw = ImageDraw.Draw(canvas)

        # --- Верхняя золотая линия и название канала ---
        draw.rectangle([0, 0, CARD_WIDTH, 10], fill=COLOR_ACCENT)

        header_font = _load_font(bold=True, size=34)
        _draw_shield(draw, MARGIN + 20, MARGIN + 12, 54, COLOR_ACCENT, width=4)
        draw.text(
            (MARGIN + 66, MARGIN - 8),
            channel_title.upper(),
            font=header_font,
            fill=COLOR_ACCENT,
        )

        # --- Плашка с местом события ---
        place = _format_place(region, city)
        content_top = MARGIN + 92

        if place:
            # Плашка не должна вылезать за поля: сначала уменьшаем кегль,
            # и только если даже минимальный не помогает — обрезаем текст.
            max_badge_width = CARD_WIDTH - 2 * MARGIN
            pin_size = 34
            padding = 22
            gap = 12

            badge_font = None
            for size in (34, 31, 28, 25):
                badge_font = _load_font(bold=True, size=size)
                needed = (
                    padding + pin_size + gap
                    + draw.textlength(place, font=badge_font) + padding
                )
                if needed <= max_badge_width:
                    break

            available_for_text = (
                max_badge_width - padding * 2 - pin_size - gap
            )
            while (
                draw.textlength(place, font=badge_font) > available_for_text
                and len(place) > 4
            ):
                place = place[:-2].rstrip(" ·,-") + "…"

            text_width = draw.textlength(place, font=badge_font)
            badge_height = 62
            badge_width = padding + pin_size + gap + text_width + padding

            draw.rounded_rectangle(
                [MARGIN, content_top,
                 MARGIN + badge_width, content_top + badge_height],
                radius=14, fill=COLOR_BADGE_BG,
            )
            _draw_map_pin(
                draw, MARGIN + padding, content_top + 14, pin_size, COLOR_BADGE_TEXT
            )

            text_bbox = badge_font.getbbox(place)
            text_y = content_top + (badge_height - (text_bbox[3] - text_bbox[1])) // 2 - text_bbox[1]
            draw.text(
                (MARGIN + padding + pin_size + gap, text_y),
                place, font=badge_font, fill=COLOR_BADGE_TEXT,
            )
            content_top += badge_height + 44

        # --- Основной текст: подбираем кегль под доступную высоту ---
        footer_height = 150
        available_width = CARD_WIDTH - 2 * MARGIN
        available_height = CARD_HEIGHT - content_top - footer_height - MARGIN

        body_font, lines, line_height = _fit_text(
            text, draw, available_width, available_height
        )

        # Центрируем блок по вертикали в отведённой области: короткая
        # новость иначе прижимается к верху и полкарточки пустует.
        block_height = len(lines) * line_height
        y = content_top + max(0, (available_height - block_height) // 2)

        for line in lines:
            draw.text((MARGIN, y), line, font=body_font, fill=COLOR_TEXT)
            y += line_height

        # --- Нижняя зона: дата и источник ---
        footer_y = CARD_HEIGHT - MARGIN - 56
        draw.line(
            [(MARGIN, footer_y - 28), (CARD_WIDTH - MARGIN, footer_y - 28)],
            fill=(255, 255, 255, 40), width=2,
        )

        meta_font = _load_font(bold=False, size=30)
        date_label = _format_date(published_at)
        if date_label:
            draw.text((MARGIN, footer_y), date_label, font=meta_font, fill=COLOR_TEXT_MUTED)

        if source_name:
            source_label = source_name
            if len(source_label) > 42:
                source_label = source_label[:41] + "…"
            source_width = draw.textlength(source_label, font=meta_font)
            draw.text(
                (CARD_WIDTH - MARGIN - source_width, footer_y),
                source_label, font=meta_font, fill=COLOR_TEXT_MUTED,
            )

        # --- Сохранение ---
        if output_path is None:
            digest = hashlib.sha256(
                f"{text}|{region}|{city}|{published_at}".encode("utf-8")
            ).hexdigest()[:20]
            output_path = IMAGES_DIR / f"card_{digest}.jpg"

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, format="JPEG", quality=92, optimize=True)

        logger.info("Карточка нарисована: %s (%d строк)", output_path.name, len(lines))
        return str(output_path)

    except Exception:
        # Оформление не должно ронять пайплайн — логируем и идём дальше.
        logger.exception("Не удалось нарисовать карточку")
        return None
