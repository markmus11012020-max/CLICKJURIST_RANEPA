"""HTTP-слой модуля чат-бота."""

from __future__ import annotations

from chatbot.api.dependencies import get_chatbot_container, get_orchestrator
from chatbot.api.routes import router

__all__ = ["get_chatbot_container", "get_orchestrator", "router"]
