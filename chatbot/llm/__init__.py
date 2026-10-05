"""LLM-слой чат-бота: промпты и шлюз к моделям."""

from __future__ import annotations

from chatbot.llm.gateway import ChatLLMGateway, NullLLMGateway
from chatbot.llm.prompts import (
    NO_CONTEXT_REPLY,
    OFF_TOPIC_REPLY,
    SYSTEM_PROMPT,
)

__all__ = [
    "ChatLLMGateway",
    "NO_CONTEXT_REPLY",
    "NullLLMGateway",
    "OFF_TOPIC_REPLY",
    "SYSTEM_PROMPT",
]
