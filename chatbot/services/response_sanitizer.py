"""Подготовка текста ответа к отправке клиенту.

Ответы чат-бота показываются посетителю, поэтому проходят через
санитайзер: он убирает мусор LLM, ограничивает длину и оставляет
только безопасную подмножество Markdown.
"""
from __future__ import annotations

import re

#: Паттерны, которые модель генерирует сверх нужного.
_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")
_HTML_ENTITY_RE = re.compile(r"&(nbsp|lt|gt|amp|quot|#\d{1,5});")
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?", re.MULTILINE)
_LEADING_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_EXCESS_BLANKS_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


class ResponseSanitizer:
    """Нормализация и усечение текста ответа.

    Правила намеренно консервативны: лучше убрать лишнее, чем испортить
    юридический текст с цитатами на нормы.
    """

    def __init__(self, max_length: int = 2000) -> None:
        self._max_length = max_length

    def clean(self, text: str) -> str:
        """Очистить текст ответа."""
        if not text:
            return ""
        result = text.replace("\r\n", "\n").replace("\r", "\n")
        result = _THINK_BLOCK_RE.sub("", result)
        result = _CODE_FENCE_RE.sub("", result)
        result = _HTML_ENTITY_RE.sub(" ", result)
        result = _HTML_TAG_RE.sub("", result)
        result = _LEADING_HEADING_RE.sub("**", result)
        result = _MULTI_SPACE_RE.sub(" ", result)
        result = _EXCESS_BLANKS_RE.sub("\n\n", result)
        return result.strip()

    def limit(self, text: str) -> str:
        """Обрезать текст по границе абзаца, не разрывая предложение."""
        if len(text) <= self._max_length:
            return text
        clipped = text[: self._max_length]
        cut = clipped.rfind("\n")
        if cut > self._max_length * 0.5:
            clipped = clipped[:cut]
        clipped = clipped.rstrip(" ,;:-—")
        return f"{clipped}…"

    def process(self, text: str) -> str:
        """Полный конвейер: очистка и ограничение длины."""
        return self.limit(self.clean(text))
