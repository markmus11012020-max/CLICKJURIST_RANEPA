"""Прикладной слой чат-бота: сценарии общения с посетителем."""
from __future__ import annotations

from chatbot.services.greeting import Greeting, GreetingDismissPolicy, GreetingService
from chatbot.services.intent_resolver import IntentMatch, IntentResolver
from chatbot.services.orchestrator import ChatOrchestrator
from chatbot.services.response_sanitizer import ResponseSanitizer

__all__ = [
    "ChatOrchestrator",
    "Greeting",
    "GreetingDismissPolicy",
    "GreetingService",
    "IntentMatch",
    "IntentResolver",
    "ResponseSanitizer",
]
