"""Pydantic-модели HTTP-контракта чат-бота.

Транспортный слой намеренно тонкий: DTO только описывают формат,
а вся логика живёт в :mod:`chatbot.services`.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from chatbot.config import WidgetPosition


class QuickReplyModel(BaseModel):
    """Кнопка быстрого ответа в JSON API."""

    id: str
    label: str


class ActionModel(BaseModel):
    """Кнопка-действие в JSON API."""

    id: str
    label: str
    url: str | None = None
    anchor: str | None = None


class BotReplyModel(BaseModel):
    """Ответ бота в JSON API."""

    text: str
    quick_replies: list[QuickReplyModel] = Field(default_factory=list)
    actions: list[ActionModel] = Field(default_factory=list)
    intent: str = "fallback"
    source: str = "scripted"
    created_at: str | None = None


class GreetingResponse(BaseModel):
    """Ответ на ``GET /api/chatbot/greeting``.

    Отдаётся сразу при открытии страницы: не ждёт LLM, поэтому виджет
    показывает приветствие мгновенно и без затрат.
    """

    session_id: str
    text: str
    quick_replies: list[QuickReplyModel] = Field(default_factory=list)
    is_returning: bool = False
    disclaimer: str = ""
    services: list[str] = Field(default_factory=list)


class MessageRequest(BaseModel):
    """Тело ``POST /api/chatbot/message``."""

    session_id: str = Field(..., min_length=8, max_length=128)
    message: str = Field(..., min_length=1, max_length=2000)


class MessageResponse(BaseModel):
    """Ответ на сообщение пользователя."""

    session_id: str
    reply: BotReplyModel
    turn_count: int = 0


class StreamMessageRequest(BaseModel):
    """Тело ``POST /api/chatbot/stream`` (то же, что у :class:`MessageRequest`)."""

    session_id: str = Field(..., min_length=8, max_length=128)
    message: str = Field(..., min_length=1, max_length=2000)
    intent_hint: str | None = Field(None, max_length=64)


class WidgetConfigResponse(BaseModel):
    """Публичная конфигурация виджета для клиента."""

    enabled: bool = True
    position: WidgetPosition = "bottom-right"
    auto_open: bool = True
    greeting_delay_ms: int = 1200
    remember_close: bool = True
    max_message_length: int = 1000
    disclaimer: str = ""
    brand: str = "КликЮрист"
    greeting_text: str = ""


class HealthResponse(BaseModel):
    """Состояние модуля чат-бота."""

    status: str
    enabled: bool
    llm_available: bool
    conversations: int
    knowledge_articles: int
    llm_provider: str = "none"


class ErrorResponse(BaseModel):
    """Единый формат ошибки модуля."""

    error: str
    code: str = "chatbot_error"


class HistoryResponse(BaseModel):
    """Восстановленная история диалога (с нуля — после очистки)."""

    session_id: str
    messages: list[dict] = Field(default_factory=list)
