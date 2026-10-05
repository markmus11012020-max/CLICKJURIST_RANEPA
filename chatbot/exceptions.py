"""Иерархия исключений модуля чат-бота.

Отдельный файл, потому что прикладной код должен ловить ошибки модуля,
не импортируя при этом FastAPI: так сервисы остаются тестируемыми
и переносимыми в другой транспорт.
"""
from __future__ import annotations


class ChatbotError(Exception):
    """Базовая ошибка чат-бота."""


class ChatbotDisabledError(ChatbotError):
    """Модуль выключен настройкой ``CHATBOT_ENABLED=false``."""


class MessageTooLongError(ChatbotError):
    """Сообщение длиннее ``max_message_length``."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"Сообщение длиннее допустимых {limit} символов. Сократите, пожалуйста."
        )
        self.limit = limit


class EmptyMessageError(ChatbotError):
    """Пустое сообщение от клиента."""


class LLMUnavailableError(ChatbotError):
    """Ни один LLM-провайдер не смог ответить.

    Обрабатывается оркестратором: пользователь получает ответ из базы
    знаний, а не сообщение об ошибке.
    """


class RateLimitExceededError(ChatbotError):
    """Превышен лимит сообщений на одну сессию."""

    def __init__(self, limit: int, retry_after_s: int = 60) -> None:
        super().__init__(
            f"Слишком много сообщений подряд. Подождите {retry_after_s} секунд."
        )
        self.limit = limit
        self.retry_after_s = retry_after_s
