"""База знаний о сервисе ClickJurist."""

from __future__ import annotations

from chatbot.knowledge.base import StaticKnowledgeBase, normalize
from chatbot.knowledge.catalog import (
    ARTICLES,
    DISCLAIMER,
    FALLBACK_TEXT,
    GREETING_QUICK_REPLIES,
    GREETING_RETURNING_TEXT,
    GREETING_TEXT,
    PACKAGE_TIERS,
    RETURNING_QUICK_REPLIES,
    SERVICE_STEPS,
    SITE_SECTIONS,
    Article,
)

__all__ = [
    "ARTICLES",
    "Article",
    "DISCLAIMER",
    "FALLBACK_TEXT",
    "GREETING_QUICK_REPLIES",
    "GREETING_RETURNING_TEXT",
    "GREETING_TEXT",
    "PACKAGE_TIERS",
    "RETURNING_QUICK_REPLIES",
    "SERVICE_STEPS",
    "SITE_SECTIONS",
    "StaticKnowledgeBase",
    "normalize",
]
