"""Доменный слой чат-бота: сущности, перечисления и контракты."""

from __future__ import annotations

from chatbot.domain.entities import (
    Action,
    BotReply,
    ChatMessage,
    Conversation,
    QuickReply,
)
from chatbot.domain.enums import AnswerSource, MessageRole
from chatbot.domain.interfaces import (
    Clock,
    ConversationRepository,
    KnowledgeArticle,
    KnowledgeBase,
    LLMGateway,
    SystemClock,
)

__all__ = [
    "Action",
    "AnswerSource",
    "BotReply",
    "ChatMessage",
    "Clock",
    "Conversation",
    "ConversationRepository",
    "KnowledgeArticle",
    "KnowledgeBase",
    "LLMGateway",
    "MessageRole",
    "QuickReply",
    "SystemClock",
]
