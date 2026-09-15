"""WEB-ФАКТЧЕКИНГ: независимая проверка правовых норм (раздел 3 ТЗ).

Требование: Stage 2 обязан проверить актуальность норм по **от 2 до 3
независимых источников**. Модуль поддерживает несколько поисковых провайдеров
и всегда приводит результат к единому виду :class:`WebSource`.

Провайдеры (``WEB_SEARCH_PROVIDER``):
    * ``yandex`` — Yandex Search API (контур РФ, рекомендуется для продакшена);
    * ``serper`` — Google через serper.dev;
    * ``tavily``  — Tavily Search API (отдаёт готовые фрагменты);
    * ``none``    — поиск отключён (модель работает без внешних ссылок).

Проверка «независимости» источников реализована в :func:`_deduplicate`:
источники сравниваются по домену, поэтому в итоговую выдачу попадают
действительно разные площадки (например, pravo.gov.ru и consultant.ru).
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from backend.config import settings


@dataclass
class WebSource:
    """Единое представление веб-источника, подтверждающего правовую норму."""

    title: str
    url: str
    snippet: str = ""
    provider: str = ""

    def as_dict(self) -> dict[str, str]:
        """Представление для API/промпта (только необходимые поля)."""
        return {"title": self.title, "url": self.url}

    @property
    def domain(self) -> str:
        """Домен источника — ключ проверки независимости."""
        try:
            return urlparse(self.url).netloc.lower()
        except ValueError:
            return ""


# Публичный псевдоним: пайплайн (`llm_chain`) работает с типом ``Source``.
Source = WebSource


def _deduplicate(sources: list[WebSource], limit: int) -> list[WebSource]:
    """Убрать повторы и ограничить выдачу ``limit`` независимыми источниками.

    Независимость = уникальный домен. Внутри домена остаётся первый результат.
    """
    seen: set[str] = set()
    unique: list[WebSource] = []
    for source in sources:
        if not source.url:
            continue
        domain = source.domain or source.url
        if domain in seen:
            continue
        seen.add(domain)
        unique.append(source)
        if len(unique) >= limit:
            break
    return unique


# --- Провайдер 1: Yandex Search API ------------------------------------------
def _search_yandex(query: str, limit: int) -> list[WebSource]:
    """Поиск через Yandex Search API (контур РФ)."""
    api_key = settings.YANDEX_SEARCH_API_KEY.strip()
    if not api_key:
        return []
    try:
        response = requests.post(
            settings.YANDEX_SEARCH_URL,
            headers={
                "Authorization": f"Api-Key {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": {
                    "searchType": "SEARCH_TYPE_RU",
                    "queryText": query[:400],
                },
                "groupSpec": {
                    "groupsOnPage": limit,
                    "docsInGroup": 1,
                },
            },
            timeout=15,
        )
        if response.status_code >= 400:
            return []
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    sources: list[WebSource] = []
    for group in data.get("groups", []) or []:
        for doc in group.get("documents", []) or []:
            url = str(doc.get("url", "")).strip()
            if not url:
                continue
            passages = doc.get("passages") or []
            snippet = " ".join(str(item.get("text", "")) for item in passages)
            sources.append(
                WebSource(
                    title=str(doc.get("title", "") or url),
                    url=url,
                    snippet=snippet.strip(),
                    provider="yandex",
                )
            )
    return sources


# --- Провайдер 2: serper.dev (Google) ----------------------------------------
def _search_serper(query: str, limit: int) -> list[WebSource]:
    """Поиск через serper.dev (структура ответа Google SERP)."""
    api_key = settings.SERPER_API_KEY.strip()
    if not api_key:
        return []
    try:
        response = requests.post(
            settings.SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query[:400], "num": limit, "gl": "ru", "hl": "ru"},
            timeout=15,
        )
        if response.status_code >= 400:
            return []
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    sources: list[WebSource] = []
    for item in data.get("organic", []) or []:
        url = str(item.get("link", "")).strip()
        if not url:
            continue
        sources.append(
            WebSource(
                title=str(item.get("title", "") or url),
                url=url,
                snippet=str(item.get("snippet", "")),
                provider="serper",
            )
        )
    return sources


# --- Провайдер 3: Tavily -----------------------------------------------------
def _search_tavily(query: str, limit: int) -> list[WebSource]:
    """Поиск через Tavily API (готовые фрагменты под RAG-сценарии)."""
    api_key = settings.TAVILY_API_KEY.strip()
    if not api_key:
        return []
    try:
        response = requests.post(
            settings.TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query[:400],
                "max_results": limit,
                "search_depth": "advanced",
                "include_answer": False,
            },
            timeout=20,
        )
        if response.status_code >= 400:
            return []
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    sources: list[WebSource] = []
    for item in data.get("results", []) or []:
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        sources.append(
            WebSource(
                title=str(item.get("title", "") or url),
                url=url,
                snippet=str(item.get("content", ""))[:1200],
                provider="tavily",
            )
        )
    return sources


# --- Публичный API -----------------------------------------------------------
_SEARCHERS = {
    "yandex": _search_yandex,
    "serper": _search_serper,
    "tavily": _search_tavily,
}


def search(query: str, min_sources: int = 2, max_sources: int = 3) -> list[WebSource]:
    """Найти независимые источники, подтверждающие правовые нормы.

    Args:
        query: поисковый запрос (обезличенное резюме проблемы).
        min_sources: минимально требуемое число независимых источников.
        max_sources: максимальное число источников в выдаче.

    Returns:
        Список независимых источников. Пустой список — это валидный сценарий:
        в этом случае Stage 2 обязан работать без внешних ссылок
        (правило конфликта данных, раздел 3 ТЗ).
    """
    provider = settings.WEB_SEARCH_PROVIDER
    if not settings.ENABLE_WEB_SEARCH or provider == "none" or not query.strip():
        return []

    searcher = _SEARCHERS.get(provider)
    if searcher is None:
        return []

    # Запрашиваем с запасом, чтобы после дедупликации осталось нужное число
    raw = searcher(query, max(max_sources * 3, 8))
    return _deduplicate(raw, max_sources)


def gather_sources(
    query: str,
    min_sources: int | None = None,
    max_sources: int | None = None,
) -> list[Source]:
    """Собрать независимые источники для Stage 2 (публичный вход пайплайна).

    Обёртка над :func:`search`, которая берёт границы числа источников из
    настроек (``WEB_SEARCH_MIN_SOURCES`` / ``WEB_SEARCH_MAX_SOURCES``), как
    требует раздел 2 ТЗ: «не менее двух и не более трёх независимых источников».

    Args:
        query: обезличенное резюме правовой проблемы (без персональных данных).
        min_sources: переопределить минимальное число источников.
        max_sources: переопределить максимальное число источников.

    Returns:
        Список источников (0–3). Пустой список — валидный сценарий
        «конфликт данных»: модель обязана отвечать без внешних ссылок.
    """
    limit_min = settings.WEB_SEARCH_MIN_SOURCES if min_sources is None else min_sources
    limit_max = settings.WEB_SEARCH_MAX_SOURCES if max_sources is None else max_sources
    limit_max = max(limit_max, limit_min, 1)
    return search(query, min_sources=limit_min, max_sources=limit_max)


def contour_status() -> dict[str, object]:
    """Состояние поискового контура — для эндпоинта /api/health."""
    provider = settings.WEB_SEARCH_PROVIDER
    key_by_provider = {
        "yandex": settings.YANDEX_SEARCH_API_KEY,
        "serper": settings.SERPER_API_KEY,
        "tavily": settings.TAVILY_API_KEY,
    }
    configured = provider in key_by_provider and bool(key_by_provider[provider].strip())
    return {
        "provider": provider,
        "enabled": settings.ENABLE_WEB_SEARCH,
        "configured": configured,
        "min_sources": settings.WEB_SEARCH_MIN_SOURCES,
        "max_sources": settings.WEB_SEARCH_MAX_SOURCES,
    }