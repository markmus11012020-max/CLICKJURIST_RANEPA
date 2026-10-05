"""HTTP-маршруты модуля чат-бота.

Слой намеренно «тонкий»: валидирует вход, вызывает оркестратор и
сериализует ответ. Бизнес-логики здесь нет — она в
:mod:`chatbot.services.orchestrator`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from chatbot.api.dependencies import (
    get_chatbot_settings,
    get_orchestrator,
    require_enabled,
)
from chatbot.config import ChatbotSettings
from chatbot.exceptions import (
    EmptyMessageError,
    MessageTooLongError,
)
from chatbot.knowledge import catalog
from chatbot.llm.streaming import StreamEvent
from chatbot.models import (
    BotReplyModel,
    ErrorResponse,
    GreetingResponse,
    HealthResponse,
    HistoryResponse,
    MessageRequest,
    MessageResponse,
    StreamMessageRequest,
    WidgetConfigResponse,
)
from chatbot.services.orchestrator import ChatOrchestrator
from chatbot.session import SessionIdentity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chatbot", tags=["chatbot"])

_identity = SessionIdentity()


def _session_key(client_session_id: str) -> str:
    """Проверить и хэшировать клиентский идентификатор сессии."""
    if not SessionIdentity.is_valid(client_session_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Некорректный идентификатор сессии",
        )
    return _identity.derive(client_session_id)


def _to_model(reply) -> BotReplyModel:
    """Преобразовать доменный ответ в DTO."""
    return BotReplyModel(
        text=reply.text,
        quick_replies=[{"id": qr.id, "label": qr.label} for qr in reply.quick_replies],
        actions=[action.to_dict() for action in reply.actions],
        intent=reply.intent,
        source=reply.source.value,
        created_at=reply.created_at.isoformat(),
    )


# ------------------------------------------------------------------------------
# Приветствие
# ------------------------------------------------------------------------------
@router.get(
    "/greeting",
    response_model=GreetingResponse,
    responses={503: {"model": ErrorResponse}},
    summary="Приветствие чат-бота",
)
async def get_greeting(
    session_id: str = Query(..., min_length=8, max_length=128),
    orchestrator: ChatOrchestrator = Depends(get_orchestrator),
    settings: ChatbotSettings = Depends(require_enabled),
) -> GreetingResponse:
    """Отдать приветствие и быстрые ответы.

    Не обращается к языковой модели: виджет получает текст сразу при
    открытии страницы.
    """
    key = _session_key(session_id)
    greeting = orchestrator.greet(key)
    return GreetingResponse(
        session_id=session_id,
        text=greeting.text,
        quick_replies=[{"id": qr.id, "label": qr.label} for qr in greeting.quick_replies],
        is_returning=greeting.is_returning,
        disclaimer=catalog.DISCLAIMER,
        services=[step.title for step in catalog.SERVICE_STEPS],
    )


# ------------------------------------------------------------------------------
# Диалог
# ------------------------------------------------------------------------------
@router.post(
    "/message",
    response_model=MessageResponse,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Отправить сообщение боту",
)
async def post_message(
    payload: MessageRequest,
    intent_hint: str | None = Query(None, max_length=64),
    orchestrator: ChatOrchestrator = Depends(get_orchestrator),
    settings: ChatbotSettings = Depends(require_enabled),
) -> MessageResponse:
    """Обработать реплику и вернуть ответ бота."""
    key = _session_key(payload.session_id)
    try:
        reply = orchestrator.handle_message(key, payload.message, intent_hint=intent_hint)
    except EmptyMessageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MessageTooLongError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return MessageResponse(
        session_id=payload.session_id,
        reply=_to_model(reply),
        turn_count=orchestrator.turn_count(key),
    )


# ------------------------------------------------------------------------------
# Потоковый ответ (SSE)
# ------------------------------------------------------------------------------
@router.post(
    "/stream",
    summary="Потоковый ответ чат-бота (SSE)",
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def post_stream(
    payload: StreamMessageRequest,
    orchestrator: ChatOrchestrator = Depends(get_orchestrator),
    settings: ChatbotSettings = Depends(require_enabled),
) -> StreamingResponse:
    """Отдать ответ по частям в формате Server-Sent Events.

    Формат кадра — как у основного пайплайна: ``data: {json}\\n\\n``.
    Типы событий: ``meta`` (намерение и источник), ``delta`` (фрагмент
    текста), ``done`` (финальный ответ), ``error``.

    Генерация выполняется в пуле потоков: провайдеры блокирующие, и
    синхронный цикл в event loop заблокировал бы весь сервис.
    """
    key = _session_key(payload.session_id)
    cleaned = payload.message.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Пустое сообщение")

    # Ошибки валидации в оркестраторе поднимаются уже в потоке, поэтому
    # проверяем их здесь, чтобы отдать код 400, а не обрыв SSE.
    if len(cleaned) > settings.max_message_length:
        raise HTTPException(
            status_code=400,
            detail=f"Сообщение длиннее {settings.max_message_length} символов",
        )

    async def event_stream() -> AsyncIterator[str]:
        """Поток кадров SSE."""
        try:
            events = orchestrator.stream_message(key, cleaned, intent_hint=payload.intent_hint)
            # Провайдеры блокирующие: каждый next() выполняется в потоке,
            # иначе event loop встанет на всё время генерации.
            async for event in iterate_in_threadpool(events):
                yield event.to_sse()
        except (EmptyMessageError, MessageTooLongError) as exc:
            yield StreamEvent.error(str(exc)).to_sse()
        except Exception:  # noqa: BLE001 — клиент не должен видеть traceback
            logger.exception("Сбой потокового ответа чат-бота")
            yield StreamEvent.error(
                "Не удалось получить ответ. Попробуйте переформулировать вопрос."
            ).to_sse()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # иначе nginx буферизует поток
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/history",
    response_model=HistoryResponse,
    summary="История диалога",
)
async def get_history(
    session_id: str = Query(..., min_length=8, max_length=128),
    orchestrator: ChatOrchestrator = Depends(get_orchestrator),
    settings: ChatbotSettings = Depends(require_enabled),
) -> HistoryResponse:
    """Вернуть историю текущего диалога (пустой список — если её нет)."""
    key = _session_key(session_id)
    messages = orchestrator.history(key)
    return HistoryResponse(
        session_id=session_id,
        messages=[m.to_dict() for m in messages],
    )


@router.post(
    "/reset",
    response_model=HistoryResponse,
    summary="Начать диалог заново",
)
async def post_reset(
    session_id: str = Query(..., min_length=8, max_length=128),
    orchestrator: ChatOrchestrator = Depends(get_orchestrator),
    settings: ChatbotSettings = Depends(require_enabled),
) -> HistoryResponse:
    """Очистить историю диалога (кнопка «Начать заново»)."""
    key = _session_key(session_id)
    orchestrator.reset(key)
    return HistoryResponse(session_id=session_id, messages=[])


# ------------------------------------------------------------------------------
# Конфигурация и состояние
# ------------------------------------------------------------------------------
@router.get(
    "/config",
    response_model=WidgetConfigResponse,
    summary="Конфигурация виджета",
)
async def get_widget_config(
    settings: ChatbotSettings = Depends(get_chatbot_settings),
) -> WidgetConfigResponse:
    """Публичные настройки виджета: без секретов и внутренних путей."""
    return WidgetConfigResponse(
        enabled=settings.enabled,
        auto_open=settings.auto_open,
        greeting_delay_ms=settings.greeting_delay_ms,
        remember_close=settings.remember_close,
        max_message_length=settings.max_message_length,
        disclaimer=catalog.DISCLAIMER,
        brand="КликЮрист",
        greeting_text=catalog.GREETING_TEXT,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Состояние модуля чат-бота",
)
async def chatbot_health(
    settings: ChatbotSettings = Depends(get_chatbot_settings),
    orchestrator: ChatOrchestrator = Depends(get_orchestrator),
) -> HealthResponse:
    """Диагностика модуля: LLM, диалоги, объём базы знаний."""
    return HealthResponse(
        status="ok" if settings.enabled else "disabled",
        enabled=settings.enabled,
        llm_available=orchestrator.llm_available(),
        conversations=orchestrator.conversation_count(),
        knowledge_articles=orchestrator.knowledge_article_count(),
        llm_provider=orchestrator.llm_provider(),
    )
