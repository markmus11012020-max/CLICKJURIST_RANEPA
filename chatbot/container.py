"""Контейнер зависимостей модуля чат-бота (composition root).

Здесь и только здесь собирается граф объектов: сервисы, репозиторий,
база знаний и LLM-шлюз. Замена реализации (например, in-memory на
Redis) — правка одного файла, а не всего модуля.

Приложение-синглтон создаётся лениво и потокобезопасно, чтобы при
импорте чат-бота не выполнялась лишняя работа.
"""
from __future__ import annotations

import logging
import threading

from chatbot.config import ChatbotSettings, chatbot_settings
from chatbot.domain.interfaces import KnowledgeBase
from chatbot.knowledge.base import StaticKnowledgeBase
from chatbot.llm.gateway import ChatLLMGateway, NullLLMGateway
from chatbot.repositories.in_memory import InMemoryConversationRepository
from chatbot.services.greeting import GreetingService
from chatbot.services.orchestrator import ChatOrchestrator

logger = logging.getLogger(__name__)


class ChatbotContainer:
    """Сборка и хранение зависимостей модуля."""

    def __init__(self, settings: ChatbotSettings | None = None) -> None:
        self.settings = settings or chatbot_settings()
        self.knowledge: KnowledgeBase = StaticKnowledgeBase()
        self.repository = InMemoryConversationRepository(
            max_conversations=self.settings.max_conversations
        )
        self.llm = self._build_llm()
        self.orchestrator = ChatOrchestrator(
            repository=self.repository,
            knowledge=self.knowledge,
            llm=self.llm,
            greeting=GreetingService(),
            max_message_length=self.settings.max_message_length,
            max_history_messages=self.settings.max_history_messages,
            type_chunk_size=self.settings.typing_chunk_size,
            type_delay_s=self.settings.typing_delay_ms / 1000.0,
        )

    def _build_llm(self):
        """Собрать LLM-шлюз; при выключенном LLM — заглушку."""
        if not self.settings.llm_enabled:
            logger.info("Чат-бот: LLM выключен, работаем на базе знаний")
            return NullLLMGateway()
        return ChatLLMGateway(
            model=self.settings.resolved_llm_model,
            temperature=self.settings.llm_temperature,
            max_tokens=self.settings.llm_max_tokens,
        )

    def purge(self) -> int:
        """Удалить протухшие диалоги. Вызывается фоновой задачей."""
        return self.repository.purge_expired(self.settings.resolved_conversation_ttl)


_container: ChatbotContainer | None = None
_lock = threading.Lock()


def get_container() -> ChatbotContainer:
    """Вернуть контейнер приложения, создав его при первом обращении."""
    global _container
    if _container is None:
        with _lock:
            if _container is None:
                _container = ChatbotContainer()
    return _container


def set_container(container: ChatbotContainer | None) -> None:
    """Подменить контейнер (используется в тестах)."""
    global _container
    with _lock:
        _container = container
