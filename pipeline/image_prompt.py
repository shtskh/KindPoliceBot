"""
Составление промпта для генератора изображений по тексту новости.

ЗАЧЕМ ОТДЕЛЬНЫЙ ШАГ
-------------------
Подставлять текст новости в генератор напрямую бесполезно: модели
рисуют по визуальному описанию, а новость написана канцелярским языком
про процессуальные действия. На запрос «сотрудники ОМВД в рамках
профилактического мероприятия оказали содействие» генератор нарисует
что угодно.

Поэтому сцену сочиняет языковая модель (она у провайдера работает, в
отличие от генерации картинок): читает новость и описывает КАДР —
место, время суток, погоду, что происходит в этот момент.

ГРАНИЦЫ, КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ
---------------------------------
Тема ведомственная, поэтому в промпте жёстко запрещено:

  * узнаваемые лица — это не фотография конкретных людей, и выдавать
    нарисованных людей за участников события нельзя;
  * точное воспроизведение формы, шевронов, гербов и номеров машин —
    официальная символика не должна подделываться;
  * текст и надписи на изображении — генераторы пишут бессмыслицу,
    а на ведомственной картинке это выглядит особенно скверно;
  * кровь, травмы, тела, оружие в действии — канал про добрые дела.

Сгенерированная картинка в любом случае помечается надписью
«Иллюстрация, сгенерировано ИИ» (см. pipeline/image_render.py).
"""
from __future__ import annotations

import re

from config import settings
from logging_setup import get_logger
from pipeline.ai_client import AIProviderError, chat

logger = get_logger("image_prompt")

# Сколько текста новости отдаём модели: сцена берётся из первых абзацев,
# дальше обычно идут процессуальные подробности.
MAX_NEWS_CHARS = 1200

# Потолок длины промпта. Генераторы обрезают длинные описания, а лишние
# детали размывают кадр.
MAX_PROMPT_CHARS = 900

SYSTEM_PROMPT = """
Ты — художник-постановщик, который по новости придумывает ОДИН кадр
для иллюстрации к посту.

Верни описание кадра на АНГЛИЙСКОМ языке, одним абзацем, 40-70 слов.
Только описание сцены, без пояснений и без кавычек.

ЧТО ОПИСЫВАТЬ
- Место действия из новости: двор жилого дома, зимняя трасса, подъезд,
  берег реки, лесная тропа, городская улица.
- Время суток и погоду, если они понятны из текста.
- Что происходит в кадре: действие, а не портрет.
- Атмосферу: спокойная, рабочая, человечная.
- Русские реалии: панельные дома, «Лада», берёзы, зимняя слякоть.

ОБЯЗАТЕЛЬНЫЕ ОГРАНИЧЕНИЯ (нарушать нельзя)
- Люди только со спины, издалека или не в фокусе. Никаких узнаваемых
  лиц и портретов крупным планом.
- Форму описывай так: "dark navy blue Russian police uniform, peaked
  cap, shoulder boards". {insignia_rule}
- Не описывай номера машин.
- Никакой крови, травм, тел, оружия в действии, огнестрельных ран.
- Не придумывай деталей, которых нет в новости.

СТИЛЬ
Документальная фотография, естественное освещение, спокойные цвета,
средний или общий план.

Если по новости кадр придумать невозможно (отчёт, статистика,
поздравление) — верни ровно одно слово: NONE
""".strip()

# Как описывать форму. Проверено живыми генерациями 10.11.2026:
# крой и цвет формы МВД генератор передаёт верно (тёмно-синий китель,
# фуражка с кокардой, погоны), а вот шеврон подделывает — надпись «МВД»
# на нём превращается в нечитаемый набор букв, а эмблема получается
# лишь похожей на настоящую. При просмотре в ленте это незаметно, при
# увеличении — сразу видно.
#
# Поэтому по умолчанию форма узнаваемая, но символика не читается.
# Переключатель AI_IMAGE_SHOW_INSIGNIA=true разрешает генератору
# рисовать шевроны и кокарды крупно — со всеми их неточностями.
_INSIGNIA_RULE_OFF = (
    "Шевроны, кокарду и надписи на форме не описывай: они должны быть "
    "неразличимы — вид со спины, с расстояния или под углом."
)
_INSIGNIA_RULE_ON = (
    "Шеврон на рукаве и кокарду на фуражке можно описать, но без текста "
    "и надписей."
)

_SUFFIX_BASE = (
    "Documentary photography, natural light, muted colors, candid moment. "
    "No recognizable faces, no close-up portraits. "
    "No blood, no injuries, no weapons."
)
_SUFFIX_NO_INSIGNIA = (
    "Officers in dark navy blue Russian police uniform with peaked cap and "
    "shoulder boards, seen from behind or at a distance so that emblems, "
    "patches and lettering are not legible. "
    "No text, no letters, no logos, no watermarks."
)
_SUFFIX_WITH_INSIGNIA = (
    "Officers in the authentic dark navy blue uniform of the Russian "
    "police: peaked cap with cockade, shoulder boards, sleeve patch. "
    "No lettering, no watermarks."
)


def prompt_suffix() -> str:
    """
    Хвост, который добавляется к любому промпту: повторяет ключевые
    требования на языке генератора, потому что часть моделей следует
    финальным указаниям охотнее, чем середине описания.
    """
    if settings.ai_image_show_insignia:
        return f"{_SUFFIX_BASE} {_SUFFIX_WITH_INSIGNIA}"
    return f"{_SUFFIX_BASE} {_SUFFIX_NO_INSIGNIA}"


def system_prompt() -> str:
    rule = (_INSIGNIA_RULE_ON if settings.ai_image_show_insignia
            else _INSIGNIA_RULE_OFF)
    return SYSTEM_PROMPT.format(insignia_rule=rule)

# Запасной вариант, если модель недоступна: хотя бы место и время года.
FALLBACK_TEMPLATE = (
    "Documentary photograph of a calm everyday street scene in a Russian "
    "town, {season}, people seen from behind at a distance. {suffix}"
)

_NONE_RE = re.compile(r"^\s*none\s*$", re.IGNORECASE)


def _season_hint(month: int) -> str:
    if month in (12, 1, 2):
        return "winter, snow on the ground"
    if month in (3, 4, 5):
        return "early spring, wet pavement"
    if month in (6, 7, 8):
        return "summer, green trees"
    return "autumn, yellow leaves"


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.strip('"').strip("'").strip()
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS].rsplit(" ", 1)[0]
    return text


def build_image_prompt(item) -> str | None:
    """
    Описание кадра для генератора. None — если иллюстрировать нечего
    (отчёт, статистика) или модель недоступна и запасной вариант
    отключён настройкой.
    """
    source_text = (item.raw_text or item.title or "").strip()
    if not source_text:
        return None

    month = getattr(getattr(item, "published_at", None), "month", 6) or 6

    if not settings.ai_configured:
        logger.info("Нет ключа AI — беру обобщённое описание кадра.")
        return FALLBACK_TEMPLATE.format(
            season=_season_hint(month), suffix=prompt_suffix()
        )

    place = " · ".join(p for p in (getattr(item, "city", None),
                                   getattr(item, "region", None)) if p)
    user_prompt = (
        f"Место события: {place or 'не указано'}\n"
        f"Время года: {_season_hint(month)}\n\n"
        f"Новость:\n{source_text[:MAX_NEWS_CHARS]}"
    )

    try:
        scene = chat(
            [
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            model=settings.ai_model,
            max_tokens=300,
            temperature=0.6,
            label="image_prompt",
        )
    except AIProviderError as exc:
        logger.warning("Не удалось сочинить кадр (%s) — беру обобщённый.", exc)
        return FALLBACK_TEMPLATE.format(
            season=_season_hint(month), suffix=prompt_suffix()
        )

    scene = _clean(scene)

    if not scene or _NONE_RE.match(scene):
        logger.info("Модель решила, что иллюстрировать нечего — будет карточка.")
        return None

    prompt = f"{scene} {prompt_suffix()}"
    logger.info("Кадр для генерации: %s", scene[:120])
    return prompt
