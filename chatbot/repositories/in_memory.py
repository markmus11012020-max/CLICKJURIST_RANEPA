"""Хранилище диалогов в памяти процесса.

Подходит для одной ноды приложения. Когда понадобится горизонтальное
масштабирование, за абстракцией
:class:`chatbot.domain.interfaces.ConversationRepository` появится
реализация на Redis — сервисы менять не придётся.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import timedelta

from chatbot.domain.entities import ChatMessage, Conversation
from chatbot.domain.interfaces import Clock, ConversationRepository, SystemClock


class InMemoryConversationRepository(ConversationRepository):
    """LRU-хранилище диалогов с протуханием по TTL.

    Диалоги содержат только текст общения в пределах сессии и удаляются
    по таймауту, что соответствует заявленному режиму Zero-Storage.
    """

    def __init__(
        self,
        max_conversations: int = 500,
        clock: Clock | None = None,
    ) -> None:
        self._max = max(1, max_conversations)
        self._clock = clock or SystemClock()
        self._items: OrderedDict[str, Conversation] = OrderedDict()
        self._lock = threading.RLock()

    # -- ConversationRepository -----------------------------------------------
    def get_or_create(self, session_id: str) -> Conversation:
        """Вернуть диалог сессии, создав пустой при первом обращении."""
        with self._lock:
            conversation = self._items.get(session_id)
            if conversation is not None:
                self._items.move_to_end(session_id)
                return conversation
            conversation = Conversation(session_id=session_id)
            self._items[session_id] = conversation
            self._evict_if_needed()
            return conversation

    def save(self, conversation: Conversation) -> None:
        """Зафиксировать изменения диалога."""
        with self._lock:
            conversation.updated_at = self._clock.now()
            self._items[conversation.session_id] = conversation
            self._items.move_to_end(conversation.session_id)
            self._evict_if_needed()

    def history(self, session_id: str, limit: int = 50) -> list[ChatMessage]:
        """История сообщений сессии (новые последними)."""
        with self._lock:
            conversation = self._items.get(session_id)
            if conversation is None:
                return []
            if limit > 0:
                return list(conversation.messages[-limit:])
            return list(conversation.messages)

    def purge_expired(self, ttl: timedelta) -> int:
        """Удалить диалоги старше ``ttl``. Вернуть количество удалённых."""
        with self._lock:
            now = self._clock.now()
            stale = [sid for sid, conv in self._items.items() if now - conv.updated_at > ttl]
            for sid in stale:
                del self._items[sid]
            return len(stale)

    # -- Служебное ------------------------------------------------------------
    def count(self) -> int:
        """Текущее количество живых диалогов (для /health)."""
        with self._lock:
            return len(self._items)

    def reset(self, session_id: str) -> bool:
        """Очистить диалог сессии (кнопка «Начать заново»)."""
        with self._lock:
            return self._items.pop(session_id, None) is not None

    def _evict_if_needed(self) -> None:
        """Вытеснить самые старые диалоги сверх лимита."""
        while len(self._items) > self._max:
            self._items.popitem(last=False)
