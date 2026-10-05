"""Перечисления доменного слоя чат-бота."""
from __future__ import annotations

from enum import Enum


class MessageRole(str, Enum):
    """Автор сообщения в диалоге."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Intent(str, Enum):
    """Намерение посетителя, распознанное по тексту сообщения.

    Значения стабильны: они попадают в телеметрию и в счётчики аналитики,
    поэтому переименование ломает исторические данные.
    """

    GREETING = "greeting"
    THANKS = "thanks"
    SMALLTALK = "smalltalk"
    PRICING = "pricing"
    PAYMENT = "payment"
    SERVICES = "services"
    STEPS = "steps"
    PRIVACY = "privacy"
    PAYWALL = "paywall"
    DOCUMENT = "document"
    CHECKLIST = "checklist"
    CONSULTATION = "consultation"
    SUPPORT = "support"
    FALLBACK = "fallback"


class AnswerSource(str, Enum):
    """Откуда взят ответ — для метрик качества и прозрачности на клиенте."""

    SCRIPTED = "scripted"  # детерминированный сценарий без модели
    KNOWLEDGE = "knowledge"  # база знаний о сервисе
    LLM = "llm"  # сгенерировано языковой моделью
    FALLBACK = "fallback"  # модель недоступна, отдан заготовленный ответ


class SenderRole(str, Enum):
    """Техническая роль, передаваемая на клиент для оформления сообщения."""

    BOT = "bot"
    USER = "user"
