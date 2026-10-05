"""Настройки модуля чат-бота.

Читаются из основного :data:`backend.config.settings`, чтобы не заводить
второй источник окружения. Все параметры имеют безопасные значения по
умолчанию: чат-бот не должен ломать работу сайта, даже если оператор
забыл прописать переменные.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from backend.config import settings as app_settings

#: Позиция плавающей кнопки относительно окна браузера.
WidgetPosition = Literal["bottom-right", "bottom-left"]


class ChatbotSettings(BaseModel):
    """Параметры виджета чат-бота."""

    # --- Включение ------------------------------------------------------------
    enabled: bool = True
    #: Показывать виджет только на указанных путях. Пустой список — на всех.
    show_on_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=lambda: ["/api", "/docs", "/static"])

    # --- Поведение приветствия ------------------------------------------------
    greeting_delay_ms: int = 1200
    """Задержка перед показом приветствия, чтобы не мешать первому действию."""

    auto_open: bool = True
    """Открывать окно чата автоматически при первом визите."""

    remember_close: bool = True
    """Не показывать приветствие повторно, если пользователь его закрыл."""

    # --- Ограничения ----------------------------------------------------------
    max_message_length: int = 1000
    max_history_messages: int = 10
    """Сколько последних сообщений уходит в LLM (контекст и цена)."""

    typing_chunk_size: int = 18
    """Размер порции при «живой печати» готового ответа, в символах."""

    typing_delay_ms: int = 20
    """Пауза между порциями при «живой печати», в миллисекундах."""

    conversation_ttl_s: int = 3600
    """Время жизни диалога в памяти: час бездействия — диалог удаляется."""

    max_conversations: int = 500
    """Потолок одновременных диалогов в памяти процесса (LRU)."""

    # --- LLM (необязательно) --------------------------------------------------
    llm_enabled: bool = True
    llm_model: str = ""
    """Модель для ответов чат-бота. Пусто — берётся из основных настроек."""

    llm_timeout_s: int = 45
    llm_max_tokens: int = 700
    llm_temperature: float = 0.3

    # -- Свойства -------------------------------------------------------------
    @property
    def resolved_llm_model(self) -> str:
        """Модель для чат-бота: явная настройка либо модель основного пайплайна."""
        return self.llm_model or app_settings.MODEL_LLM_1

    @property
    def resolved_conversation_ttl(self) -> timedelta:
        """TTL диалога как :class:`datetime.timedelta`."""
        return timedelta(seconds=self.conversation_ttl_s)


def chatbot_settings() -> ChatbotSettings:
    """Собрать настройки чат-бота из окружения проекта.

    Значения по умолчанию подставляются явно, а не через отдельную схему
    pydantic-settings: модуль остаётся опциональным и не требует правок
    в ``.env``/``.env.example``.
    """

    def _flag(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on", "да"}

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return ChatbotSettings(
        enabled=_flag("CHATBOT_ENABLED", True),
        show_on_paths=[
            p.strip() for p in os.getenv("CHATBOT_SHOW_ON_PATHS", "").split(",") if p.strip()
        ],
        exclude_paths=[
            p.strip()
            for p in os.getenv("CHATBOT_EXCLUDE_PATHS", "/api,/docs,/static,/chatbot").split(",")
            if p.strip()
        ],
        greeting_delay_ms=_int("CHATBOT_GREETING_DELAY_MS", 1200),
        auto_open=_flag("CHATBOT_AUTO_OPEN", True),
        remember_close=_flag("CHATBOT_REMEMBER_CLOSE", True),
        max_message_length=_int("CHATBOT_MAX_MESSAGE_LENGTH", 1000),
        max_history_messages=_int("CHATBOT_MAX_HISTORY_MESSAGES", 10),
        typing_chunk_size=_int("CHATBOT_TYPING_CHUNK_SIZE", 18),
        typing_delay_ms=_int("CHATBOT_TYPING_DELAY_MS", 20),
        conversation_ttl_s=_int("CHATBOT_CONVERSATION_TTL_S", 3600),
        max_conversations=_int("CHATBOT_MAX_CONVERSATIONS", 500),
        llm_enabled=_flag("CHATBOT_LLM_ENABLED", True),
        llm_model=os.getenv("CHATBOT_LLM_MODEL", "").strip(),
        llm_timeout_s=_int("CHATBOT_LLM_TIMEOUT_S", 45),
        llm_max_tokens=_int("CHATBOT_LLM_MAX_TOKENS", 700),
        llm_temperature=_float("CHATBOT_LLM_TEMPERATURE", 0.3),
    )
