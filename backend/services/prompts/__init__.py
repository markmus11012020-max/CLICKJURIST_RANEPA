"""Системные промпты ClickJurist, разложенные по доменам.

Раньше всё лежало в одном файле ``prompts.py`` на 60+ КБ. Теперь
промпты сгруппированы по назначению, а этот модуль переэкспортирует
их прежние имена — поэтому ``from backend.services.prompts import
PROMPT_LLM_1`` продолжает работать без изменений в коде.
"""

from __future__ import annotations

from backend.services.prompts.analysis import PROMPT_STAGE2_ANALYSIS
from backend.services.prompts.checklist import PROMPT_CHECKLIST
from backend.services.prompts.disclaimer import (
    AI_DISCLAIMER,
    LEGAL_DISCLAIMER_DYNAMIC,
    with_dynamic_disclaimer,
)
from backend.services.prompts.documents import (
    DOCUMENT_FALLBACK_RULE,
    PROMPT_CLAIM,
    PROMPT_COMPLAINT,
    PROMPT_COURT_ORDER_CANCELLATION,
    PROMPT_DOCUMENT,
    PROMPT_LAWSUIT,
)
from backend.services.prompts.draft import PROMPT_LLM_1, PROMPT_LLM_2
from backend.services.prompts.masking import PROMPT_STAGE1_MASKING
from backend.services.prompts.persona import SYSTEM_PERSONA
from backend.services.prompts.sources import (
    SOURCES_CONFLICT_NOTE,
    SOURCES_EMPTY,
)

__all__ = [
    "AI_DISCLAIMER",
    "DOCUMENT_FALLBACK_RULE",
    "LEGAL_DISCLAIMER_DYNAMIC",
    "PROMPT_CHECKLIST",
    "PROMPT_CLAIM",
    "PROMPT_COMPLAINT",
    "PROMPT_COURT_ORDER_CANCELLATION",
    "PROMPT_DOCUMENT",
    "PROMPT_LAWSUIT",
    "PROMPT_LLM_1",
    "PROMPT_LLM_2",
    "PROMPT_STAGE1_MASKING",
    "PROMPT_STAGE2_ANALYSIS",
    "SOURCES_CONFLICT_NOTE",
    "SOURCES_EMPTY",
    "SYSTEM_PERSONA",
    "with_dynamic_disclaimer",
]
