"""Зависимости FastAPI для модуля чат-бота.

Отдельный файл, чтобы роуты оставались тонкими: получение зависимости —
одна строка, вся сборка — в :mod:`chatbot.container`.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from chatbot.config import ChatbotSettings
from chatbot.container import ChatbotContainer, get_container
from chatbot.services.orchestrator import ChatOrchestrator


def get_chatbot_container(request: Request) -> ChatbotContainer:
    """Достать контейнер из состояния приложения.

    Контейнер кладётся в ``app.state`` при старте — так его можно
    подменить в тестах через ``app.dependency_overrides``.
    """
    container = getattr(request.app.state, "chatbot", None)
    if container is None:
        container = get_container()
    return container


def get_orchestrator(request: Request) -> ChatOrchestrator:
    """Оркестратор диалога."""
    return get_chatbot_container(request).orchestrator


def get_chatbot_settings(request: Request) -> ChatbotSettings:
    """Настройки модуля."""
    return get_chatbot_container(request).settings


def require_enabled(
    settings: ChatbotSettings = Depends(get_chatbot_settings),
) -> ChatbotSettings:
    """Отклонить запрос, если модуль выключен.

    Используется как ``Depends(require_enabled)``. Параметр обязан быть
    именно ``Depends``: иначе FastAPI примет ``ChatbotSettings`` за тело
    запроса и ответит 422 на GET.
    """
    if not settings.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Чат-бот временно недоступен",
        )
    return settings
