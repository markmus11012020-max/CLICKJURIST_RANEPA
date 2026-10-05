"""События потокового ответа чат-бота.

Формат на проводе — Server-Sent Events в том же виде, что и у основного
пайплайна (``data: {json}\\n\\n``), чтобы клиентский парсер был один на
оба потока.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StreamEventType(str, Enum):
    """Тип события в потоке ответа."""

    META = "meta"
    """Начало ответа: намерение, источник, что именно будет отвечать бот."""

    DELTA = "delta"
    """Фрагмент текста — эффект «живой печати»."""

    DONE = "done"
    """Финальный ответ целиком, готовый к сохранению в историю."""

    ERROR = "error"
    """Ошибка генерации; клиент показывает текст и предлагает повторить."""


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """Одно событие потока.

    ``data`` — произвольные поля события, сериализуются как есть.
    Наружу отдаётся только словарь: клиенту не нужны классы Python.
    """

    type: StreamEventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """Представить событие в формате SSE."""
        payload = {"type": self.type.value, **self.data}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # -- Конструкторы --------------------------------------------------------
    @classmethod
    def meta(cls, intent: str, source: str, streamed: bool) -> "StreamEvent":
        """Начало генерации: что бот понял и откуда возьмёт ответ."""
        return cls(
            StreamEventType.META,
            {"intent": intent, "source": source, "streamed": streamed},
        )

    @classmethod
    def delta(cls, text: str) -> "StreamEvent":
        """Фрагмент текста ответа."""
        return cls(StreamEventType.DELTA, {"text": text})

    @classmethod
    def done(cls, reply) -> "StreamEvent":
        """Финальный ответ: доменный объект :class:`BotReply`."""
        return cls(StreamEventType.DONE, {"reply": reply.to_dict()})

    @classmethod
    def error(cls, message: str) -> "StreamEvent":
        """Ошибка генерации."""
        return cls(StreamEventType.ERROR, {"message": message})
