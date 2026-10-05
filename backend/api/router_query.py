"""Консультация и фоновые задачи генерации (разделы 1 и 2.1 ТЗ)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend import task_store
from backend.api.deps import session_gate
from backend.db import store
from backend.logging_setup import setup_logging
from backend.models import (
    AsyncTaskResponse,
    AsyncTaskStatusResponse,
    GenerateResponse,
    QueryRequest,
)
from backend.services import llm_chain
from backend.services.legal_category import classify as classify_legal_category

logger = setup_logging()

router = APIRouter(prefix="/api", tags=["consultation"])


# ------------------------------------------------------------------------------
# Синхронная консультация
# ------------------------------------------------------------------------------
@router.post("/query", response_model=GenerateResponse)
async def api_query(payload: QueryRequest, request: Request) -> Response:
    """Полный цикл: маскировка ПДн → фактчекинг → анализ → эталон асессора."""
    session_hash, session_id, was_free, denial = session_gate(request, "consultation")
    if denial is not None:
        return denial

    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    result = llm_chain.run_pipeline(payload.query, with_stage2=True)

    if result.error:
        store.log_request(session_hash, "consultation", 502, was_free, result.stage2_provider)
        body = GenerateResponse(
            response=None,
            anonymized=result.anonymized,
            stage1_provider=result.stage1_provider,
            stage2_provider=result.stage2_provider,
            warning=result.warning,
            error=result.error,
            legal_category=classify_legal_category(payload.query),
        )
        return JSONResponse(status_code=502, content=body.model_dump())

    body = GenerateResponse(
        response=llm_chain.attach_disclaimer(result.final),
        sources=result.sources,  # type: ignore[arg-type]
        citation_verified=result.citation_verified,
        anonymized=result.anonymized,
        stage1_provider=result.stage1_provider,
        stage2_provider=result.stage2_provider,
        warning=result.warning,
        legal_category=classify_legal_category(payload.query),
    )
    store.log_request(session_hash, "consultation", 200, was_free, result.stage2_provider)
    response = JSONResponse(content=body.model_dump())
    response.headers["X-Session-Id"] = session_id
    return response


# ------------------------------------------------------------------------------
# Фоновая генерация (раздел 2.1 ТЗ prompt160926.md)
# ------------------------------------------------------------------------------
def _run_pipeline_task(record: task_store.TaskRecord, query: str) -> dict:
    """Обёртка пайплайна для фонового потока с прогрессом и guardrails.

    Использует стриминговый пайплайн (Шаг 1 ТЗ prompt170926.md): каждый
    токен эталонного ответа LLM-2 публикуется как SSE-событие ``token``,
    чтобы фронтенд мог рендерить «живую печать» в реальном времени.
    """
    from backend.services import guardrails

    store_obj = task_store.get_task_store()

    def _on_event(event: dict) -> None:
        # Пробрасываем события стримингового пайплайна в ленту задачи.
        record.push_event(event)
        # Обновляем прогресс для polling-эндпоинта.
        if event.get("type") == "progress":
            store_obj.update(
                record.task_id,
                stage=event.get("stage", ""),
                progress=event.get("progress", 0),
            )

    result = llm_chain.run_pipeline_streaming(query, on_event=_on_event)

    store_obj.update(record.task_id, stage="guardrails", progress=90)
    record.push_event({"type": "progress", "stage": "guardrails", "progress": 90})

    # Guardrails: проверка эталонного ответа (раздел 5.2 ТЗ).
    guard_report = guardrails.validate_consultation(
        result.final or "",
        masked_query=result.mask.masked_query if result.mask else "",
    )
    record.push_event(
        {
            "type": "guardrails",
            "passed": guard_report.passed,
            "summary": guard_report.summary(),
        }
    )

    if result.error:
        return {
            "error": result.error,
            "warning": result.warning,
            "stage1_provider": result.stage1_provider,
            "stage2_provider": result.stage2_provider,
            "legal_category": classify_legal_category(query),
        }

    return {
        "response": llm_chain.attach_disclaimer(result.final),
        "sources": result.sources,
        "citation_verified": result.citation_verified,
        "anonymized": result.anonymized,
        "stage1_provider": result.stage1_provider,
        "stage2_provider": result.stage2_provider,
        "warning": result.warning,
        "guardrails_passed": guard_report.passed,
        "legal_category": classify_legal_category(query),
    }


@router.post("/query/async", response_model=AsyncTaskResponse, status_code=202)
async def api_query_async(payload: QueryRequest, request: Request) -> Response:
    """Поставить задачу генерации в фоновую очередь (раздел 2.1 ТЗ).

    Возвращает ``202 Accepted`` с ``task_id``. Клиент опрашивает статус через
    ``GET /api/query/status/{task_id}`` или подписывается на события через
    ``GET /api/query/stream/{task_id}`` (SSE).
    """
    session_hash, session_id, was_free, denial = session_gate(request, "consultation")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    record = task_store.submit_task(_run_pipeline_task, payload.query)
    store.log_request(session_hash, "consultation_async", 202, was_free, "background")

    body = AsyncTaskResponse(
        task_id=record.task_id,
        status=record.status.value,
        message="Задача принята в обработку. Опрашивайте /api/query/status/{task_id}",
    )
    response = JSONResponse(status_code=202, content=body.model_dump())
    response.headers["X-Task-Id"] = record.task_id
    response.headers["X-Session-Id"] = session_id
    return response


@router.get("/query/status/{task_id}", response_model=AsyncTaskStatusResponse)
async def api_query_status(task_id: str) -> Response:
    """Опросить статус фоновой задачи (polling)."""
    status = task_store.get_task_status(task_id)
    if not status:
        return JSONResponse(
            status_code=404,
            content={"error": "Задача не найдена или истёк срок хранения"},
        )
    return JSONResponse(content=status)


@router.get("/query/stream/{task_id}")
async def api_query_stream(task_id: str) -> Response:
    """SSE-стриминг событий фоновой задачи (Шаг 1 ТЗ prompt170926.md).

    Формат: ``data: {json}\\n\\n``. Соединение закрывается после завершения задачи.
    Прокидывает ВСЕ события стримингового пайплайна:
        * ``started`` / ``progress`` — этапы генерации;
        * ``token`` — фрагменты текста эталонного ответа (эффект «живой печати»);
        * ``sources`` — веб-источники;
        * ``meta`` — провайдеры и предупреждения;
        * ``guardrails`` — отчёт валидации;
        * ``completed`` / ``failed`` / ``cancelled`` — финальный статус.
    """
    status = task_store.get_task_status(task_id)
    if not status:
        return JSONResponse(
            status_code=404,
            content={"error": "Задача не найдена или истёк срок хранения"},
        )

    def event_stream():
        for event in task_store.stream_task_events(task_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
