"""Сущности доменного слоя: сообщения, диалоги, ответы бота."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from chatbot.domain.enums import AnswerSource, MessageRole


def utc_now() -> datetime:
    """Текущее время в UTC с таймзоной.

    Отдельная функция, чтобы домен не зависел от реализации ``Clock`` —
    так проще тестировать (подменяем её в тестах).
    """
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class QuickReply:
    """Кнопка быстрого ответа под сообщением бота.

    ``id`` стабилен: по нему интенты сопоставляются в
    :class:`chatbot.services.intent_resolver.IntentResolver`, поэтому
    переименование ломает аналитику.
    """

    id: str
    label: str
    intent_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "intent_hint": self.intent_hint}


@dataclass(frozen=True, slots=True)
class Action:
    """Кнопка-действие в карточке ответа (переход к оплате, к разделу)."""

    id: str
    label: str
    url: str | None = None
    anchor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "anchor": self.anchor,
        }


@dataclass(frozen=True, slots=True)
class BotReply:
    """Единица ответа бота, готовая к отправке клиенту.

    Неизменяемый — репозиторий и транспортный слой не должны иметь
    возможности доработать ответ «по пути» и испортить кэш диалога.
    """

    text: str
    quick_replies: tuple[QuickReply, ...] = ()
    actions: tuple[Action, ...] = ()
    intent: str = "fallback"
    source: AnswerSource = AnswerSource.SCRIPTED
    created_at: datetime = field(default_factory=utc_now)

    @classmethod
    def scripted(
        cls,
        text: str,
        *,
        intent: str = "fallback",
        quick_replies: tuple[QuickReply, ...] = (),
        actions: tuple[Action, ...] = (),
    ) -> "BotReply":
        """Собрать ответ из сценария (без участия языковой модели)."""
        return cls(
            text=text,
            quick_replies=quick_replies,
            actions=actions,
            intent=intent,
            source=AnswerSource.SCRIPTED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "quick_replies": [qr.to_dict() for qr in self.quick_replies],
            "actions": [action.to_dict() for action in self.actions],
            "intent": self.intent,
            "source": self.source.value,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Одно сообщение в истории диалога."""

    role: MessageRole
    text: str
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "text": self.text,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChatMessage":
        """Восстановить сообщение из сериализованного вида."""
        return cls(
            role=MessageRole(raw.get("role", MessageRole.USER.value)),
            text=str(raw.get("text", "")),
            created_at=datetime.fromisoformat(str(raw.get("created_at"))),
        )


@dataclass(slots=True)
class Conversation:
    """Диалог посетителя.

    Хранится в памяти процесса и содержит **только** текст общения.
    Идентификатор сессии — псевдонимизированный SHA-256 (152-ФЗ),
    см. :mod:`backend.security`.
    """

    session_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_intent: str | None = None
    turn_count: int = 0

    def add_message(self, message: ChatMessage) -> None:
        """Дописать сообщение в историю и обновить служебные счётчики."""
        self.messages.append(message)
        self.updated_at = utc_now()
        if message.role is MessageRole.USER:
            self.turn_count += 1

    def history_for_llm(self, limit: int = 10) -> list[dict[str, str]]:
        """Последние сообщения в формате, который понимает LLM-шлюз."""
        tail = self.messages[-limit:] if limit > 0 else self.messages
        return [{"role": m.role.value, "content": m.text} for m in tail]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "messages": [m.to_dict() for m in self.messages],
            "turn_count": self.turn_count,
            "last_intent": self.last_intent,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
