"""
Дешёвый предварительный фильтр по ключевым словам — применяется СРАЗУ
после сбора из общих лент (РИА/ТАСС), до вызова LLM. Экономит API-вызовы:
из тысяч новостей в день общая лента отдаёт единицы, реально связанные
с полицией, а платить за LLM-проверку каждой из них не нужно.
"""
from storage.models import RawNewsItem


def matches_police_keywords(item: RawNewsItem, keywords: list[str]) -> bool:
    haystack = (item.title + " " + item.raw_text).lower()
    return any(keyword in haystack for keyword in keywords)


def filter_by_keywords(items: list[RawNewsItem], keywords: list[str]) -> list[RawNewsItem]:
    return [item for item in items if matches_police_keywords(item, keywords)]
