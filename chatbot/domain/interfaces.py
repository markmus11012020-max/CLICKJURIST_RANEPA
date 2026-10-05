"""Абстрактные контракты доменного слоя.

Каждая внешняя зависимость чат-бота описана здесь интерфейсом.
Благодаря этому конкретные реализации (in-memory хранилище, AITunnel,
YandexGPT, статическая база знаний) можно подменять в контейнере
:mod:`chatbot.container` без правки прикладной логики.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from chatbot.domain.entities import BotReply, ChatMessage, Conversation
from chatbot.domain.enums import Intent


class Clock(ABC):
    """Источник времени.

    Вынесен в интерфейс ради детерминированных тестов: подставив
    ``FrozenClock``, можно проверить протухание диалога без ``sleep``.
    """

    @abstractmethod
    def now(self) -> datetime:
        """Текущее время (ожидается timezone-aware UTC)."""

    def seconds_ago(self, seconds: int) -> datetime:
        """Точка времени ``seconds`` назад."""
        return self.now() - timedelta(seconds=seconds)


class SystemClock(Clock):
    """Боевая реализация :class:`Clock` поверх системных часов."""

    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)


class ConversationRepository(ABC):
    """Хранилище диалогов.

    Реализация :class:`chatbot.repositories.in_memory.InMemoryConversationRepository`
    подходит для одной ноды. Для нескольких инстансов за интерфейсом
    появится реализация на Redis — код сервисов при этом не меняется.
    """

    @abstractmethod
    def get_or_create(self, session_id: str) -> Conversation:
        """Вернуть диалог сессии, создав пустой при первом обращении."""

    @abstractmethod
    def save(self, conversation: Conversation) -> None:
        """Зафиксировать изменения диалога."""

    @abstractmethod
    def history(self, session_id: str, limit: int = 50) -> list[ChatMessage]:
        """История сообщений сессии (новые последними)."""

    @abstractmethod
    def purge_expired(self, ttl: timedelta) -> int:
        """Удалить диалоги старше ``ttl``. Вернуть количество удалённых."""


class KnowledgeArticle(ABC):
    """Статья базы знаний о сервисе."""

    @property
    @abstractmethod
    def intent(self) -> Intent:
        """Намерение, которому отвечает статья."""

    @property
    @abstractmethod
    def title(self) -> str:
        """Заголовок статьи."""

    @property
    @abstractmethod
    def body(self) -> str:
        """Текст статьи в Markdown (безопасно рендерится на клиенте)."""

    @property
    @abstractmethod
    def keywords(self) -> tuple[str, ...]:
        """Ключевые слова для поиска статьи по сообщению пользователя."""


class KnowledgeBase(ABC):
    """Источник фактов о сайте: тарифы, шаги, безопасность, документы."""

    @abstractmethod
    def find(self, intent: Intent) -> list[KnowledgeArticle]:
        """Все статьи по намерению (может быть пустым списком)."""

    @abstractmethod
    def render(self, intent: Intent) -> str:
        """Готовый текст ответа по намерению ("" — если статьи нет)."""

    @abstractmethod
    def context_for_llm(self, intent: Intent) -> str:
        """Компактный контекст, который подмешивается в промпт модели."""


class LLMGateway(ABC):
    """Шлюз к языковым моделям.

    Контракт сознательно узкий: чат-боту не нужны маскировка ПДн и
    веб-фактчекинг основного пайплайна — только генерация ответа
    по уже известным фактам о сервисе.
    """

    @abstractmethod
    def is_available(self) -> bool:
        """Готов ли шлюз отвечать (заданы ли ключи провайдера)."""

    @abstractmethod
    def answer(
        self,
        question: str,
        context: str,
        history: list[dict[str, str]],
    ) -> str | None:
        """Сгенерировать ответ или вернуть ``None``, если модель недоступна.

        ``None`` означает «модель не справилась» — вызывающий код обязан
        отдать пользователю заготовку из базы знаний, а не пустоту.
        """

    def stream_answer(
        self,
        question: str,
        context: str,
        history: list[dict[str, str]],
    ) -> Iterator[str] | None:
        """Потоковая генерация ответа.

        Возвращает итератор фрагментов текста либо ``None``, если шлюз
        не умеет стримить. Если генератор не отдал ни одного фрагмента,
        вызывающий код вправе попробовать другого провайдера.
        """
        return None

    @property
    def last_provider(self) -> str:
        """Имя провайдера, ответившего последним (для метрик)."""
        return "none"
