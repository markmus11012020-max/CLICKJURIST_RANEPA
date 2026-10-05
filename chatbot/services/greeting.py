"""Сценарий приветствия — стартовый сценарий модуля.

Приветствие отдаётся синхронно и без участия языковой модели: виджет
показывает его мгновенно при открытии страницы, а ответ на первый вопрос
уходит уже в фоне.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from chatbot.domain.entities import QuickReply
from chatbot.domain.enums import Intent
from chatbot.knowledge import catalog
from chatbot.knowledge.base import normalize


@dataclass(frozen=True, slots=True)
class Greeting:
    """Готовое приветствие для конкретного посетителя."""

    text: str
    quick_replies: tuple[QuickReply, ...]
    is_returning: bool

    @property
    def primary_intent(self) -> Intent:
        """Намерение, к которому ведёт первая кнопка."""
        if not self.quick_replies:
            return Intent.GREETING
        hint = self.quick_replies[0].intent_hint
        return Intent(hint) if hint else Intent.GREETING


class GreetingService:
    """Сборка приветствия и списка быстрых ответов.

    Зависит только от каталога знаний, поэтому полностью детерминирован
    и покрывается тестами без внешних систем.
    """

    def __init__(
        self,
        greeting_text: str = catalog.GREETING_TEXT,
        returning_text: str = catalog.GREETING_RETURNING_TEXT,
        returning_after_turns: int = 1,
    ) -> None:
        self._greeting_text = greeting_text
        self._returning_text = returning_text
        self._returning_after_turns = returning_after_turns

    def build(self, *, turn_count: int = 0) -> Greeting:
        """Собрать приветствие с учётом того, сколько реплик уже было.

        Args:
            turn_count: количество сообщений пользователя в этом диалоге.
                Значение ``0`` — первый визит.
        """
        returning = turn_count >= self._returning_after_turns
        if returning:
            return Greeting(
                text=self._returning_text,
                quick_replies=self._quick_replies(catalog.RETURNING_QUICK_REPLIES),
                is_returning=True,
            )
        return Greeting(
            text=self._greeting_text,
            quick_replies=self._quick_replies(catalog.GREETING_QUICK_REPLIES),
            is_returning=False,
        )

    @staticmethod
    def _quick_replies(
        rows: tuple[tuple[str, str, str], ...],
    ) -> tuple[QuickReply, ...]:
        """Преобразовать кортежи каталога в доменные объекты."""
        return tuple(
            QuickReply(id=row[0], label=row[1], intent_hint=row[2]) for row in rows
        )


class GreetingDismissPolicy:
    """Правило «показывать ли приветствие этому посетителю».

    Хранит отметку о закрытии окна в ``localStorage``. Решение принимается
    здесь, а не в виджете, чтобы оно оставалось единым и его можно было
    покрыть тестами: клиент только передаёт значение маркера.
    """

    STORAGE_KEY = "clickjurist.chatbot.dismissed"

    def __init__(self, dismissed: bool = False, ttl_days: int = 7) -> None:
        self._dismissed = dismissed
        self._ttl_days = ttl_days

    def should_show(self, *, remember_close: bool = True) -> bool:
        """Показать ли приветствие (учитывая режим «не запоминать»)."""
        if not remember_close:
            return True
        return not self._dismissed

    @classmethod
    def from_marker(cls, marker: str | None, *, ttl_days: int = 7) -> "GreetingDismissPolicy":
        """Разобрать значение из ``localStorage``.

        Формат маркера: ``<unix-ts>|<0|1>`` — время закрытия и признак
        «пользователь попросил не показывать снова». Просроченный маркер
        (старше ``ttl_days``) считается отсутствующим.
        """
        if not marker:
            return cls(dismissed=False, ttl_days=ttl_days)
        parts = normalize(marker).split("|")
        if len(parts) != 2 or not parts[0].isdigit():
            return cls(dismissed=False, ttl_days=ttl_days)
        closed_at = int(parts[0])
        if int(time.time()) - closed_at > ttl_days * 86_400:
            return cls(dismissed=False, ttl_days=ttl_days)
        return cls(dismissed=parts[1] == "1", ttl_days=ttl_days)

    def to_marker(self) -> str:
        """Собрать значение для ``localStorage``."""
        return f"{int(time.time())}|{'1' if self._dismissed else '0'}"
