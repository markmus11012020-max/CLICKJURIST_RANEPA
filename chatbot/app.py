"""Подключение модуля чат-бота к основному приложению FastAPI.

Всё, что модуль добавляет в ``backend.main:app``, описано здесь.
Подключение — одна строка в ``backend/main.py``::

    from chatbot.app import register
    register(app)

Модуль опционален: если он не подключён, приложение работает как раньше.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

from backend.static_files import NoCacheStaticFiles
from chatbot.api.routes import router
from chatbot.container import ChatbotContainer, get_container

logger = logging.getLogger(__name__)

#: Каталог статики виджета.
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: Префикс, по которому виджет отдаётся браузеру.
STATIC_URL_PREFIX = "/chatbot/static"


def register(
    app: FastAPI,
    *,
    container: ChatbotContainer | None = None,
    mount_static: bool = True,
) -> FastAPI:
    """Подключить чат-бот к приложению.

    Args:
        app: экземпляр FastAPI.
        container: контейнер зависимостей. По умолчанию — общий синглтон
            модуля; в тестах можно передать изолированный.
        mount_static: отдавать ли статику виджета. ``False`` — когда
            файлы раздаёт внешний CDN или nginx.

    Returns:
        Тот же ``app`` — для удобства цепочки вызовов при импорте.
    """
    resolved = container or get_container()
    app.state.chatbot = resolved

    app.include_router(router)
    if mount_static and STATIC_DIR.exists():
        app.mount(
            STATIC_URL_PREFIX,
            NoCacheStaticFiles(directory=str(STATIC_DIR)),
            name="chatbot-static",
        )
        logger.info("Чат-бот подключён: статика %s", STATIC_URL_PREFIX)

    _register_lifecycle(app, resolved)
    logger.info("Чат-бот подключён: API /api/chatbot, LLM=%s", resolved.llm.last_provider)
    return app


def _register_lifecycle(app: FastAPI, container: ChatbotContainer) -> None:
    """Очистка протухших диалогов на старте и остановке приложения.

    Регистрируется через события, а не через ``on_event``, чтобы не
    зависеть от устаревающего способа и не конфликтовать с уже
    настроенными обработчиками основного приложения.
    """
    try:
        from contextlib import asynccontextmanager

        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app_: FastAPI):
            container.purge()  # убрать диалоги, оставшиеся с прошлого запуска
            async with original_lifespan(app_) as maybe_state:
                yield maybe_state

        app.router.lifespan_context = lifespan
    except Exception as exc:  # pragma: no cover — не критично для работы сайта
        logger.warning("Не удалось зарегистрировать очистку диалогов: %s", exc)
