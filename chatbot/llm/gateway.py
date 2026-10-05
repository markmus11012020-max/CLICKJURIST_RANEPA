"""Шлюз к языковым моделям для чат-бота.

Переиспользует провайдеров основного проекта
(:mod:`backend.services.providers`) и его failover-оркестратор, поэтому
модуль не плодит собственные HTTP-клиенты и не дублирует настройки
провайдеров.

Шлюз реализует принцип «деградация вместо отказа»: любой сбой
возвращает ``None``, и оркестратор отдаёт заготовку из базы знаний.
Пользователь никогда не видит техническую ошибку.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from chatbot.domain.interfaces import LLMGateway
from chatbot.llm.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class ChatLLMGateway(LLMGateway):
    """Адаптер основного LLM-слоя под нужды чат-бота."""

    def __init__(
        self,
        model: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 700,
        enabled: bool = True,
        provider_names: tuple[str, ...] | None = None,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._enabled = enabled
        self._provider_names = provider_names
        self._last_provider = "none"

    # -- LLMGateway -----------------------------------------------------------
    def is_available(self) -> bool:
        """Доступен ли хотя бы один провайдер из цепочки."""
        if not self._enabled:
            return False
        try:
            return any(self._build_provider(name) for name in self._chain())
        except Exception:  # pragma: no cover — защита от битой установки
            return False

    def answer(
        self,
        question: str,
        context: str,
        history: list[dict[str, str]],
    ) -> str | None:
        """Сгенерировать ответ; ``None`` — модель недоступна."""
        if not self._enabled:
            return None

        messages = self._build_messages(question, context, history)
        for name in self._chain():
            provider = self._build_provider(name)
            if provider is None or not provider.is_configured():
                continue
            try:
                text = provider.chat(
                    messages=messages,
                    model=self._model,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
            except Exception as exc:
                logger.warning("LLM-провайдер %s недоступен для чат-бота: %s", name, exc)
                continue
            if not text or not text.strip():
                continue
            self._last_provider = name
            return text.strip()
        return None

    @property
    def last_provider(self) -> str:
        return self._last_provider

    def stream_answer(
        self,
        question: str,
        context: str,
        history: list[dict[str, str]],
    ) -> Iterator[str] | None:
        """Потоковая генерация с перебором провайдеров.

        Переход на резервного провайдера допускается только пока не
        отдан ни одного фрагмента: иначе пользователь увидит начало
        ответа одной модели и продолжение другой.
        """
        if not self._enabled:
            return None

        messages = self._build_messages(question, context, history)
        for name in self._chain():
            provider = self._build_provider(name)
            if provider is None or not provider.is_configured():
                continue
            produced = False
            try:
                for piece in provider.stream_chat(
                    messages=messages,
                    model=self._model,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                ):
                    if not piece:
                        continue
                    produced = True
                    self._last_provider = name
                    yield str(piece)
            except Exception as exc:
                logger.warning("Стриминг %s прерван для чат-бота: %s", name, exc)
                if produced:
                    return
                continue
            if produced:
                return
        return None

    # -- Внутреннее -----------------------------------------------------------
    @staticmethod
    def _build_provider(name: str):
        """Собрать провайдера; ``None`` — если он не зарегистрирован."""
        try:
            from backend.services.providers import get_provider
        except Exception:  # pragma: no cover — основной пакет не импортируется
            return None
        try:
            return get_provider(name)
        except Exception:  # pragma: no cover — неизвестное имя провайдера
            return None

    def _chain(self) -> tuple[str, ...]:
        """Цепочка провайдеров: заданная явно либо failover основного пайплайна."""
        if self._provider_names:
            return self._provider_names
        try:
            from backend.services.providers import fallback_chain

            return tuple(fallback_chain())
        except Exception:  # pragma: no cover — основной пакет не импортируется
            return ("aitunnel", "yandex")

    @staticmethod
    def _build_messages(
        question: str, context: str, history: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Собрать список сообщений для модели."""
        system = SYSTEM_PROMPT
        if context:
            system = f"{SYSTEM_PROMPT}\n\nКОНТЕКСТ О СЕРВИСЕ (источник фактов):\n{context}"
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in history
            if m.get("content")
        )
        messages.append({"role": "user", "content": question})
        return messages


class NullLLMGateway(LLMGateway):
    """Заглушка: чат-бот работает целиком на базе знаний.

    Используется в тестах и при ``CHATBOT_LLM_ENABLED=false`` — модуль
    остаётся полностью функциональным без внешних ключей.
    """

    def is_available(self) -> bool:
        return False

    def answer(
        self,
        question: str,
        context: str,
        history: list[dict[str, str]],
    ) -> str | None:
        return None

    def stream_answer(
        self,
        question: str,
        context: str,
        history: list[dict[str, str]],
    ) -> Iterator[str] | None:
        return None
