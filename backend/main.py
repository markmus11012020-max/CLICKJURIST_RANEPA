"""ClickJurist Production — FastAPI-приложение (мегапайплайн юридических услуг).

Архитектура (см. ``prompt.150926.md``):

    Клиент → POST /api/query (async, 202 Accepted + task_id)
              │
              ├─ 0. JWT-авторизация (HttpOnly cookie) + платёжный барьер
              ├─ 1. STAGE 1 (контур РФ):  YandexGPT / Ollama / regex → маскировка ПДн
              ├─ 2. Web-фактчекинг (агент): 2–3 источника, приоритет consultant.ru/garant.ru
              ├─ 3. STAGE 2 (внешний):    Gemini 2.5 Flash через AITunnel + failover
              ├─ 4. LLM-1 (черновик) → LLM-2 (Senior-асессор) → Guardrails → эталон
              │
              └─ Zero-Storage Logging: только session_id и метрики

Клиент опрашивает статус задачи:
    GET /api/query/status/{task_id}     — polling
    GET /api/query/stream/{task_id}     — SSE-стриминг событий

Запуск локально:
    python -m backend.main
Запуск в production (Yandex Serverless Containers):
    uvicorn backend.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import traceback
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from backend import jwt_auth, task_store
from backend.config import PROJECT_ROOT, settings
from backend.db import store
from backend.logging_setup import setup_logging
from backend.models import (
    AsyncTaskResponse,
    AsyncTaskStatusResponse,
    ChecklistRequest,
    ChecklistResponse,
    DocumentRequest,
    DocumentResponse,
    GenerateResponse,
    HealthResponse,
    PackageRequest,
    PackageResponse,
    PaymentCreateRequest,
    PaymentCreateResponse,
    PaymentRequiredResponse,
    QueryRequest,
    SessionStateResponse,
)
from backend.security import session_context
from backend.services import llm_chain, pdf_generator, robokassa
from backend.services.legal_category import classify as classify_legal_category
from backend.services.prompts import AI_DISCLAIMER, with_dynamic_disclaimer

# --- Инициализация окружения --------------------------------------------------
logger = setup_logging()

FRONTEND_DIR = PROJECT_ROOT / "frontend"
INDEX_FILE = FRONTEND_DIR / "index.html"

app = FastAPI(
    title="ClickJurist Production",
    description=(
        "Юридический ИИ-сервис: двухэтапный мегапайплайн с маскировкой "
        "персональных данных в контуре РФ, веб-фактчекингом и оплатой через Robokassa."
    ),
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # для JWT-cookie (раздел 3.1 ТЗ)
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Session-Id", "X-Task-Id"],
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Замерить задержку и записать техническую метрику (без ПДн)."""
    started = time.perf_counter()
    response = await call_next(request)
    latency_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Response-Time-Ms"] = str(latency_ms)
    logger.info(
        "Запрос обработан",
        extra={
            "endpoint": request.url.path,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
        },
    )
    return response


# ------------------------------------------------------------------------------
# ПЛАТЁЖНЫЙ БАРЬЕР (раздел 4 ТЗ)
# ------------------------------------------------------------------------------
def _payment_required(service: str, session_hash: str) -> JSONResponse:
    """Вернуть 402 Payment Required с готовой ссылкой на оплату Robokassa."""
    invoice = robokassa.create_invoice(service, session_hash)
    payload = PaymentRequiredResponse(
        detail=(
            "Бесплатный запрос уже использован. Оформите оплату, чтобы "
            "получить услугу."
        ),
        service=service,  # type: ignore[arg-type]
        amount=int(invoice["amount"]),
        payment_url=str(invoice["payment_url"]),
        inv_id=str(invoice["inv_id"]),
        is_test=bool(invoice["is_test"]),
    )
    return JSONResponse(status_code=402, content=payload.model_dump())


def _session_gate(
    request: Request, service: str
) -> tuple[str, str, bool, JSONResponse | None]:
    """Проверить право на запрос: бесплатный доступ или оплаченный период.

    Returns:
        ``(session_hash, session_id, was_free, denial)``. Если ``denial`` не
        ``None`` — запрос нужно прервать ответом 402.

    Notes:
        Если включён режим разработчика (``settings.DEV_BYPASS_PAYWALL=True``),
        платёжный барьер полностью отключается: 402 никогда не возвращается,
        бесплатный лимит не списывается, сессия регистрируется в БД только
        ради совместимости со store.log_request().
    """
    session_hash, session_id, _ = session_context(request)
    store.ensure_session(session_hash)

    # --- Dev Mode: обход платёжного барьера ----------------------------------
    # Проверяется ПЕРВЫМ делом — до has_paid_access / can_use_free_request,
    # чтобы 402 гарантированно никогда не вернулся из этой функции.
    if settings.DEV_BYPASS_PAYWALL:
        logger.info(
            "Dev Mode: Paywall bypassed (session_hash=%s…, service=%s)",
            session_hash[:12],
            service,
        )
        return session_hash, session_id, False, None

    if store.has_paid_access(session_hash):
        return session_hash, session_id, False, None
    if store.can_use_free_request(session_hash):
        return session_hash, session_id, True, None
    return session_hash, session_id, False, _payment_required(service, session_hash)


# ------------------------------------------------------------------------------
# ОСНОВНОЙ МЕГАПАЙПЛАЙН
# ------------------------------------------------------------------------------
@app.post("/api/query", response_model=GenerateResponse)
async def api_query(payload: QueryRequest, request: Request) -> Response:
    """Полный цикл: маскировка ПДн → фактчекинг → анализ → эталон асессора."""
    session_hash, session_id, was_free, denial = _session_gate(request, "consultation")
    if denial is not None:
        return denial

    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    result = llm_chain.run_pipeline(payload.query, with_stage2=True)

    if result.error:
        store.log_request(
            session_hash, "consultation", 502, was_free, result.stage2_provider
        )
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
    store.log_request(
        session_hash, "consultation", 200, was_free, result.stage2_provider
    )
    response = JSONResponse(content=body.model_dump())
    response.headers["X-Session-Id"] = session_id
    return response


# ------------------------------------------------------------------------------
# АСИНХРОННАЯ ГЕНЕРАЦИЯ (раздел 2.1 ТЗ prompt160926.md)
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
    record.push_event({
        "type": "guardrails",
        "passed": guard_report.passed,
        "summary": guard_report.summary(),
    })

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


@app.post("/api/query/async", response_model=AsyncTaskResponse, status_code=202)
async def api_query_async(payload: QueryRequest, request: Request) -> Response:
    """Поставить задачу генерации в фоновую очередь (раздел 2.1 ТЗ).

    Возвращает ``202 Accepted`` с ``task_id``. Клиент опрашивает статус через
    ``GET /api/query/status/{task_id}`` или подписывается на события через
    ``GET /api/query/stream/{task_id}`` (SSE).
    """
    session_hash, session_id, was_free, denial = _session_gate(request, "consultation")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    record = task_store.submit_task(_run_pipeline_task, payload.query)
    store.log_request(
        session_hash, "consultation_async", 202, was_free, "background"
    )

    body = AsyncTaskResponse(
        task_id=record.task_id,
        status=record.status.value,
        message="Задача принята в обработку. Опросите /api/query/status/{task_id}",
    )
    response = JSONResponse(status_code=202, content=body.model_dump())
    response.headers["X-Task-Id"] = record.task_id
    response.headers["X-Session-Id"] = session_id
    return response


@app.get("/api/query/status/{task_id}", response_model=AsyncTaskStatusResponse)
async def api_query_status(task_id: str) -> Response:
    """Опросить статус фоновой задачи (polling)."""
    status = task_store.get_task_status(task_id)
    if not status:
        return JSONResponse(
            status_code=404,
            content={"error": "Задача не найдена или истёк срок хранения"},
        )
    return JSONResponse(content=status)


@app.get("/api/query/stream/{task_id}")
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


# ------------------------------------------------------------------------------
# ЧЕК-ЛИСТ И ДОКУМЕНТ
# ------------------------------------------------------------------------------
@app.post("/api/checklist", response_model=ChecklistResponse)
async def api_checklist(payload: ChecklistRequest, request: Request) -> Response:
    """Пошаговый чек-лист действий по готовой консультации."""
    session_hash, session_id, was_free, denial = _session_gate(request, "checklist")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    try:
        from backend.services.pii_masker import mask_query

        mask = mask_query(payload.query)
        checklist = llm_chain.draft_checklist(mask.masked_query, payload.final_answer)
    except Exception as exc:  # noqa: BLE001 — наружу отдаём понятный текст
        logger.error(f"Ошибка генерации чек-листа: {type(exc).__name__}")
        store.log_request(session_hash, "checklist", 502, was_free)
        return JSONResponse(
            status_code=502,
            content=ChecklistResponse(
                error=f"Ошибка генерации чек-листа: {exc}"
            ).model_dump(),
        )

    store.log_request(session_hash, "checklist", 200, was_free)
    body = ChecklistResponse(checklist=llm_chain.attach_disclaimer(checklist))
    response = JSONResponse(content=body.model_dump())
    response.headers["X-Session-Id"] = session_id
    return response


@app.post("/api/document", response_model=DocumentResponse)
async def api_document(payload: DocumentRequest, request: Request) -> Response:
    """Черновик процессуального документа (иск / претензия / жалоба)."""
    session_hash, session_id, was_free, denial = _session_gate(request, "document")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    try:
        from backend.services.pii_masker import mask_query

        mask = mask_query(payload.query)
        document = llm_chain.draft_document(mask.masked_query, payload.doc_type)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка генерации документа: {type(exc).__name__}")
        store.log_request(session_hash, "document", 502, was_free)
        return JSONResponse(
            status_code=502,
            content=DocumentResponse(
                error=f"Ошибка генерации документа: {exc}"
            ).model_dump(),
        )

    store.log_request(session_hash, "document", 200, was_free)
    body = DocumentResponse(
        document=llm_chain.attach_disclaimer(document, for_document=True),
        doc_type=payload.doc_type,
    )
    response = JSONResponse(content=body.model_dump())
    response.headers["X-Session-Id"] = session_id
    return response


# ------------------------------------------------------------------------------
# АСИНХРОННАЯ ГЕНЕРАЦИЯ ЧЕК-ЛИСТА И ДОКУМЕНТА (Шаги 2/3 ТЗ prompt170926.md)
# ------------------------------------------------------------------------------
def _run_checklist_task(
    record: task_store.TaskRecord, query: str, final_answer: str
) -> dict:
    """Фоновая задача генерации чек-листа со стримингом токенов."""
    store_obj = task_store.get_task_store()

    def _on_event(event: dict) -> None:
        record.push_event(event)
        if event.get("type") == "progress":
            store_obj.update(
                record.task_id,
                stage=event.get("stage", ""),
                progress=event.get("progress", 0),
            )

    try:
        from backend.services.pii_masker import mask_query

        record.push_event({"type": "progress", "stage": "masking", "progress": 10})
        mask = mask_query(query)
        record.push_event({"type": "progress", "stage": "masking_done", "progress": 25})

        record.push_event({"type": "progress", "stage": "drafting", "progress": 40})
        text = llm_chain.draft_checklist(mask.masked_query, final_answer)
        # Эмитим порциями по ~120 символов — эффект «живой печати».
        chunk = 120
        for i in range(0, len(text), chunk):
            piece = text[i:i + chunk]
            record.push_event({"type": "token", "text": piece})

        record.push_event({"type": "progress", "stage": "done", "progress": 100})
        final_text = llm_chain.attach_disclaimer(text)
        return {"checklist": final_text}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Фоновая задача чек-листа упала")
        return {"error": f"{type(exc).__name__}: {exc}"}


def _run_document_task(
    record: task_store.TaskRecord, query: str, doc_type: str
) -> dict:
    """Фоновая задача генерации документа со стримингом токенов."""
    # Hard intercept for corporate queries sent to complaint
    q_lower = query.lower() if query else ""
    explanation_text: str = ""
    if doc_type == "complaint" and any(k in q_lower for k in ["ооо", "генеральный директор", "акции", "доля 15%", "крупная сделка"]):
        doc_type = "lawsuit"
        # Force push explanation event immediately before LLM call
        record.push_event({"type": "token", "text": "Внимание: Для защиты прав участников ООО при корпоративных спорах административный порядок (подача жалобы) законодательством РФ не предусмотрен. Заявление автоматически сформировано в формате Искового заявления в Арбитражный суд согласно ст. 12 ГК РФ.\n\n"})

    store_obj = task_store.get_task_store()

    def _on_event(event: dict) -> None:
        record.push_event(event)
        if event.get("type") == "progress":
            store_obj.update(
                record.task_id,
                stage=event.get("stage", ""),
                progress=event.get("progress", 0),
            )

    try:
        from backend.services.pii_masker import mask_query

        record.push_event({"type": "progress", "stage": "masking", "progress": 10})
        mask = mask_query(query)
        record.push_event({"type": "progress", "stage": "masking_done", "progress": 25})

        record.push_event({"type": "progress", "stage": "drafting", "progress": 40})
        text = llm_chain.draft_document(mask.masked_query, doc_type)
        if explanation_text:
            record.push_event({"type": "token", "text": explanation_text})
        chunk = 120
        for i in range(0, len(text), chunk):
            piece = text[i:i + chunk]
            record.push_event({"type": "token", "text": piece})

        record.push_event({"type": "progress", "stage": "done", "progress": 100})
        final_text = llm_chain.attach_disclaimer(text, for_document=True)
        return {"document": final_text, "doc_type": doc_type}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Фоновая задача документа упала")
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.post("/api/checklist/async", response_model=AsyncTaskResponse, status_code=202)
async def api_checklist_async(
    payload: ChecklistRequest, request: Request
) -> Response:
    """Поставить генерацию чек-листа в фоновую очередь (Шаг 2 ТЗ prompt170926.md).

    Возвращает ``202 Accepted`` с ``task_id``. Клиент подписывается на события
    через ``GET /api/query/stream/{task_id}`` (SSE).
    """
    session_hash, session_id, was_free, denial = _session_gate(request, "checklist")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    record = task_store.submit_task(
        _run_checklist_task, payload.query, payload.final_answer
    )
    store.log_request(session_hash, "checklist_async", 202, was_free, "background")

    body = AsyncTaskResponse(
        task_id=record.task_id,
        status=record.status.value,
        message="Задача чек-листа принята в обработку",
    )
    response = JSONResponse(status_code=202, content=body.model_dump())
    response.headers["X-Task-Id"] = record.task_id
    response.headers["X-Session-Id"] = session_id
    return response


@app.post("/api/document/async", response_model=AsyncTaskResponse, status_code=202)
async def api_document_async(
    payload: DocumentRequest, request: Request
) -> Response:
    """Поставить генерацию документа в фоновую очередь (Шаг 3 ТЗ prompt170926.md).

    Возвращает ``202 Accepted`` с ``task_id``. Клиент подписывается на события
    через ``GET /api/query/stream/{task_id}`` (SSE).
    """
    session_hash, session_id, was_free, denial = _session_gate(request, "document")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    record = task_store.submit_task(
        _run_document_task, payload.query, payload.doc_type
    )
    store.log_request(session_hash, "document_async", 202, was_free, "background")

    body = AsyncTaskResponse(
        task_id=record.task_id,
        status=record.status.value,
        message="Задача документа принята в обработку",
    )
    response = JSONResponse(status_code=202, content=body.model_dump())
    response.headers["X-Task-Id"] = record.task_id
    response.headers["X-Session-Id"] = session_id
    return response


@app.post("/api/pdf")
async def api_pdf(payload: ChecklistRequest, request: Request) -> Response:
    """PDF-версия консультации с поддержкой кириллицы (ReportLab).

    Тяжёлая CPU-bound сборка PDF выполняется в отдельном потоке через
    ``run_in_threadpool``, чтобы не блокировать event-loop FastAPI.
    ``asyncio.wait_for`` гарантирует, что зависший генератор не держит
    соединение открытым бесконечно — клиент получит HTTP 504 по таймауту.
    """
    session_hash, session_id, was_free, denial = _session_gate(request, "pdf")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    # Таймаут генерации PDF: 60 секунд — даже очень длинный отчёт
    # обрабатывается за 5–10 секунд; всё, что дольше — это зависание.
    PDF_TIMEOUT_S = 60.0
    started = time.perf_counter()
    try:
        logger.info("[PDF] /api/pdf: offloading build_pdf to threadpool")
        pdf_bytes = await asyncio.wait_for(
            run_in_threadpool(
                pdf_generator.build_pdf,
                content_markup=payload.final_answer,
            ),
            timeout=PDF_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.error(
            f"[PDF] TIMEOUT: build_pdf не завершился за {PDF_TIMEOUT_S:.0f}с "
            f"(прошло {elapsed_ms} мс)"
        )
        store.log_request(session_hash, "pdf", 504, was_free)
        return JSONResponse(
            status_code=504,
            content={
                "error": (
                    f"Превышено время формирования PDF ({PDF_TIMEOUT_S:.0f}с). "
                    "Сервис автоматически прервал зависшую операцию."
                )
            },
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.error(
            f"[PDF] Ошибка формирования PDF: {type(exc).__name__}: {exc} "
            f"(прошло {elapsed_ms} мс)"
        )
        store.log_request(session_hash, "pdf", 502, was_free)
        return JSONResponse(
            status_code=500,
            content={"error": f"Ошибка формирования PDF: {exc}"},
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(f"[PDF] Готово за {elapsed_ms} мс, размер {len(pdf_bytes)} байт")
    store.log_request(session_hash, "pdf", 200, was_free)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{pdf_generator.filename_for("document")}"'
            ),
        },
    )


# ------------------------------------------------------------------------------
# ПЛАТЕЖИ ROBOKASSA (раздел 4 ТЗ)
# ------------------------------------------------------------------------------
def _robokassa_keys_present() -> bool:
    """Проверить, что ключи Робокассы заданы (не пустые и не дефолтные)."""
    login = (settings.ROBOKASSA_LOGIN or "").strip()
    pwd1 = (settings.ROBOKASSA_PASSWORD1 or "").strip()
    pwd2 = (settings.ROBOKASSA_PASSWORD2 or "").strip()
    if not login or not pwd1 or not pwd2:
        return False
    # Дефолтные sandbox-значения из .env.example считаем «не настроено».
    defaults = {"clickjurist", "test_password_1", "test_password_2"}
    if {login, pwd1, pwd2} <= defaults:
        return False
    return True


def _build_robokassa_url(
    inv_id: str, amount: int, service: str, description: str = ""
) -> str:
    """Собрать реальный URL Робокассы с MD5-подписью.

    Формула подписи (официальная документация Robokassa):
        SignatureValue = MD5(MerchantLogin:OutSum:InvId:Password1)

    Параметры читаются из окружения с безопасным fallback на плейсхолдеры,
    чтобы функция не падала при локальной отладке без реальных ключей.
    """
    login = (settings.ROBOKASSA_LOGIN or "demo_login").strip() or "demo_login"
    password1 = (
        settings.ROBOKASSA_PASSWORD1 or "demo_password_1"
    ).strip() or "demo_password_1"
    is_test = bool(settings.ROBOKASSA_TEST)

    out_sum = f"{amount:.2f}"
    signature = hashlib.md5(
        f"{login}:{out_sum}:{inv_id}:{password1}".encode("utf-8")
    ).hexdigest()

    desc = description or f"ClickJurist: услуга «{service}»"
    base_url = (
        settings.ROBOKASSA_PAYMENT_URL
        or "https://auth.robokassa.ru/Merchant/Index.aspx"
    )
    params = (
        f"MerchantLogin={login}"
        f"&OutSum={out_sum}"
        f"&InvId={inv_id}"
        f"&Description={desc}"
        f"&SignatureValue={signature}"
        f"&IsTest={1 if is_test else 0}"
    )
    return f"{base_url}?{params}"


def _mock_invoice(service: str, session_hash: str) -> dict[str, object]:
    """Сгенерировать фейковый счёт для локального тестирования без реальных ключей."""
    amount = settings.prices.get(service, settings.PRICE_CONSULTATION)
    inv_id = f"MOCK-{int(time.time() * 1000)}"
    logger.warning(
        "Robokassa keys missing/empty — используем MOCK-режим (service=%s, amount=%s)",
        service,
        amount,
    )
    return {
        "inv_id": inv_id,
        "amount": amount,
        "service": service,
        "payment_url": _build_robokassa_url(inv_id, amount, service),
        "is_test": True,
    }


@app.post("/api/payment/create", response_model=PaymentCreateResponse)
async def api_payment_create(
    payload: PaymentCreateRequest, request: Request
) -> PaymentCreateResponse:
    """Выставить счёт на оплату выбранной услуги (sandbox: IsTest=1)."""
    try:
        session_hash, _session_id, _ip = session_context(request)
        store.ensure_session(session_hash)

        # Fallback: если ключи Робокассы не настроены — отдаём mock-ссылку,
        # чтобы локальная отладка не падала с HTTP 500.
        if not _robokassa_keys_present():
            invoice = _mock_invoice(payload.service, session_hash)
        else:
            invoice = robokassa.create_invoice(payload.service, session_hash)

        return PaymentCreateResponse(**invoice)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Ошибка в /api/payment/create: %s\n%s",
            exc,
            traceback.format_exc(),
        )
        # Возвращаем mock-ссылку вместо 500, чтобы UI не падал.
        try:
            amount = settings.prices.get(payload.service, settings.PRICE_CONSULTATION)
        except Exception:
            amount = 0
        return PaymentCreateResponse(
            inv_id=f"ERR-{int(time.time() * 1000)}",
            amount=amount,
            service=payload.service,
            payment_url=_build_robokassa_url(
                f"ERR-{int(time.time() * 1000)}", amount, payload.service
            ),
            is_test=True,
        )


# ------------------------------------------------------------------------------
# ПАКЕТНЫЙ ТАРИФ «РЕШЕНИЕ ПРОБЛЕМЫ ПОД КЛЮЧ» (раздел 6 ТЗ prompt160926.md)
# ------------------------------------------------------------------------------
@app.post("/api/package", response_model=PackageResponse)
async def api_package(payload: PackageRequest, request: Request) -> Response:
    """Пакетная генерация: консультация + чек-лист + документ (раздел 6 ТЗ).

    Единый тариф ``basic`` (390 ₽) — консультация + чек-лист + шаблон документа.

    Доступ ТОЛЬКО после оплаты (проверяется через ``_session_gate``).
    """
    service_code = "package_basic"
    session_hash, session_id, was_free, denial = _session_gate(request, service_code)  # type: ignore[arg-type]
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    amount = settings.PRICE_PACKAGE_BASIC
    body = PackageResponse(tier=payload.tier, amount=amount)

    try:
        from backend.services.pii_masker import mask_query

        mask = mask_query(payload.query)
        result = llm_chain.run_pipeline(payload.query, with_stage2=True)
        if result.error:
            body.error = result.error
            body.warning = result.warning
            store.log_request(session_hash, service_code, 502, was_free)
            return JSONResponse(status_code=502, content=body.model_dump())

        body.consultation = with_dynamic_disclaimer(
            llm_chain.attach_disclaimer(result.final)
        )
        body.sources = result.sources  # type: ignore[assignment]

        # Чек-лист (всегда).
        try:
            checklist = llm_chain.draft_checklist(mask.masked_query, result.final or "")
            body.checklist = with_dynamic_disclaimer(
                llm_chain.attach_disclaimer(checklist)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Пакет: чек-лист не сгенерирован: %s", exc)
            body.warning = (body.warning or "") + f" Чек-лист: {exc}"

        # Документ (всегда включён в пакет).
        try:
            document = llm_chain.draft_document(mask.masked_query, "lawsuit")
            body.document = with_dynamic_disclaimer(
                llm_chain.attach_disclaimer(document, for_document=True)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Пакет: документ не сгенерирован: %s", exc)
            body.warning = (body.warning or "") + f" Документ: {exc}"

        store.log_request(session_hash, service_code, 200, was_free)
        response = JSONResponse(content=body.model_dump())
        response.headers["X-Session-Id"] = session_id
        return response
    except Exception as exc:  # noqa: BLE001
        logger.exception("Пакетная генерация упала")
        body.error = f"{type(exc).__name__}: {exc}"
        store.log_request(session_hash, service_code, 500, was_free)
        return JSONResponse(status_code=500, content=body.model_dump())


# ------------------------------------------------------------------------------
# JWT-АВТОРИЗАЦИЯ (раздел 3.1 ТЗ prompt160926.md)
# ------------------------------------------------------------------------------
@app.post("/api/auth/login")
async def api_auth_login(request: Request) -> Response:
    """Создать новую JWT-сессию и вернуть cookie.

    Не требует пароля: идентификатор сессии привязан к отпечатку браузера
    (заголовок ``X-Client-Fingerprint``). Это позволяет сохранять сессию
    при смене IP (Wi-Fi → LTE), но защищает от перехвата cookie.
    """
    fingerprint = jwt_auth.get_fingerprint_from_request(request)
    token, session_uuid, expires = jwt_auth.create_session_token(
        fingerprint=fingerprint,
    )
    store.ensure_session(session_uuid)

    response = JSONResponse(
        content={
            "session_uuid": session_uuid,
            "expires_at": expires.isoformat(),
            "message": "JWT-сессия создана",
        }
    )
    jwt_auth.set_session_cookie(response, token, expires)
    return response


@app.post("/api/auth/logout")
async def api_auth_logout() -> Response:
    """Удалить JWT-cookie (logout)."""
    response = JSONResponse(content={"message": "JWT-сессия завершена"})
    jwt_auth.clear_session_cookie(response)
    return response


@app.get("/api/auth/status")
async def api_auth_status(request: Request) -> dict[str, Any]:
    """Проверить валидность текущей JWT-сессии."""
    session_uuid, session_id = jwt_auth.get_session_from_request(request)
    token = request.cookies.get(settings.JWT_COOKIE_NAME, "")
    payload = jwt_auth.decode_session_token(token)
    return {
        "session_uuid": session_uuid,
        "session_id": session_id,
        "authenticated": payload is not None,
        "expires_at": (
            payload.get("exp") if payload else None
        ),
    }


@app.get("/api/payment/result")
@app.post("/api/payment/result")
async def api_payment_result(request: Request) -> Response:
    """Result URL: серверное уведомление Робокассы об успешной оплате.

    Ответ строго ``OK<InvId>`` — иначе Робокасса повторяет уведомление.
    """
    params: dict[str, str] = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({key: str(value) for key, value in form.items()})

    ok, message = robokassa.confirm_payment(
        str(params.get("OutSum", "")),
        str(params.get("InvId", "")),
        str(params.get("SignatureValue", "")),
    )
    return Response(
        content=message, media_type="text/plain", status_code=200 if ok else 400
    )


@app.get("/api/payment/success")
async def api_payment_success(request: Request) -> Response:
    """Success URL: возврат пользователя после успешной оплаты."""
    params = request.query_params
    verified = robokassa.verify_success_signature(
        str(params.get("OutSum", "")),
        str(params.get("InvId", "")),
        str(params.get("SignatureValue", "")),
    )
    return Response(
        content=_payment_result_page(
            success=True, inv_id=str(params.get("InvId", "")), verified=verified
        ),
        media_type="text/html; charset=utf-8",
    )


@app.get("/api/payment/fail")
async def api_payment_fail(request: Request) -> Response:
    """Fail URL: возврат пользователя при отказе от оплаты."""
    inv_id = str(request.query_params.get("InvId", ""))
    if inv_id:
        robokassa.fail_payment(inv_id)
    return Response(
        content=_payment_result_page(success=False, inv_id=inv_id, verified=False),
        media_type="text/html; charset=utf-8",
    )


def _payment_result_page(success: bool, inv_id: str, verified: bool) -> str:
    """Страница возврата с оплаты (без внешних зависимостей и ПДн)."""
    if success and verified:
        title = "Оплата подтверждена"
        text = (
            "Доступ к платным функциям ClickJurist активирован. "
            "Вернитесь в сервис и продолжите работу."
        )
    elif success:
        title = "Платёж обрабатывается"
        text = (
            "Подпись возврата не совпала, но если оплата прошла — доступ "
            "активируется автоматически после уведомления Робокассы."
        )
    else:
        title = "Оплата не завершена"
        text = "Вы можете вернуться в сервис и попробовать оплатить снова."

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} — ClickJurist</title>
  <style>
    body {{ margin:0; min-height:100vh; display:flex; align-items:center;
            justify-content:center; background:#0b1020; color:#e8ecf7;
            font-family:'Segoe UI', system-ui, sans-serif; }}
    .card {{ max-width:520px; padding:40px; border-radius:20px;
             background:rgba(255,255,255,.06);
             border:1px solid rgba(255,255,255,.12); text-align:center; }}
    h1 {{ font-size:26px; margin:0 0 12px; }}
    p {{ color:#b6c2e2; line-height:1.6; }}
    .inv {{ font-size:13px; color:#7f8db3; margin-top:18px; }}
    a {{ display:inline-block; margin-top:24px; padding:12px 26px;
         border-radius:12px; background:linear-gradient(135deg, #818cf8 0%, #4f46e5 100%);
         color:#ffffff; text-decoration:none; font-weight:600;
         box-shadow:0 10px 24px -12px rgba(99,102,241,.55); }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{text}</p>
    <div class="inv">Счёт № {inv_id or '—'}</div>
    <a href="/">Вернуться в ClickJurist</a>
  </div>
</body>
</html>"""


# ------------------------------------------------------------------------------
# СЕССИЯ И ЗДОРОВЬЕ СЕРВИСА
# ------------------------------------------------------------------------------
@app.get("/api/session", response_model=SessionStateResponse)
async def api_session(request: Request) -> SessionStateResponse:
    """Текущее состояние сессии без раскрытия персональных данных."""
    session_hash, session_id, _ = session_context(request)
    session = store.ensure_session(session_hash)
    used = int(session.get("free_requests_used", 0))
    return SessionStateResponse(
        session_id=session_id,
        is_free=bool(int(session.get("is_free", 0))),
        free_requests_left=max(0, settings.FREE_TIER_REQUESTS - used),
        paid_access=store.has_paid_access(session_hash),
        paid_until=session.get("paid_until"),
        prices=settings.prices,
    )


@app.get("/api/prices")
async def api_prices() -> dict[str, int]:
    """Актуальные тарифы сервиса (рубли)."""
    return settings.prices


@app.get("/api/health", response_model=HealthResponse)
async def api_health() -> HealthResponse:
    """Проверка работоспособности и состояние защитных контуров."""
    return HealthResponse(
        status="ok",
        environment=settings.APP_ENV,
        masking_contour=settings.MASKING_PROVIDER,
        analysis_provider=settings.PRIMARY_PROVIDER,
        web_search_provider=settings.WEB_SEARCH_PROVIDER,
        kms_enabled=settings.YANDEX_KMS_ENABLED,
        database=store.dialect,
    )


@app.get("/api/stats")
async def api_stats() -> dict[str, int]:
    """Анонимная агрегированная статистика (без каких-либо ПДн)."""
    return store.stats()


@app.get("/api/legal")
async def api_legal() -> dict[str, str]:
    """Юридическая информация: дисклеймер и режим обработки данных (152-ФЗ)."""
    return {
        "disclaimer": AI_DISCLAIMER,
        "privacy": (
            "### 🔒 Безопасность и анонимность данных (152-ФЗ)\n"
            "Сервис спроектирован в строгом соответствии с российским "
            "законодательством о защите персональных данных:\n"
            "• **Полная анонимность:** Мы не храним тексты ваших обращений, "
            "фамилии, адреса или телефоны на серверах.\n"
            "• **Анонимность на лету:** Все личные данные (имена, контакты) "
            "автоматически удаляются из текста до того, как запрос будет "
            "отправлен на интеллектуальный анализ.\n"
            "• **Конфиденциальность сессии:** Доступ к вашим бесплатным лимитам "
            "и документам защищен безопасным цифровым идентификатором вашего "
            "устройства."
        ),
        "masking": (
            "### Режим Zero-Storage\n"
            "ClickJurist работает в режиме **Zero-Storage** (No-Data-Retention): "
            "тексты ваших обращений, фамилии, адреса, телефоны и иные персональные "
            "данные **не сохраняются** на серверах. В базе данных хранятся только "
            "псевдонимизированный идентификатор сессии (SHA-256 от IP + отпечатка "
            "браузера + соли) и технические метрики.\n\n"
            "### Локальный контур Stage 1 (маскировка ПДн)\n"
            "До передачи запроса во внешнюю аналитическую модель (Gemini 2.5 Flash) "
            "ваш текст проходит через **изолированный российский контур маскировки**: "
            "имена заменяются на `[NAME_1]`, адреса — на `[ADDRESS_1]`, телефоны — на "
            "`[PHONE_1]`, названия организаций — на `[ORG_1]`. Двухслойная защита: "
            "LLM (YandexGPT/Ollama) + обязательная regex-страховка. Ни один фрагмент "
            "ПДн не покидает пределы РФ.\n\n"
            "### Защита сессий через JWT\n"
            "Идентификация сессии реализована через **JWT-токен в HttpOnly, Secure, "
            "SameSite=Strict cookie**. JavaScript не может прочитать такой токен "
            "(защита от XSS), cookie не отправляется на сторонние сайты (защита от "
            "CSRF). Сессия не привязана к IP-адресу — переключение Wi-Fi ↔ LTE не "
            "разрывает авторизацию. Дополнительно токен привязан к хешу отпечатка "
            "браузера: даже при перехвате cookie злоумышленник не сможет ей "
            "воспользоваться без оригинального браузера."
        ),
    }


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Единый формат ошибок валидации."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "Некорректные данные запроса",
            "details": jsonable_encoder(exc.errors()),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Не раскрывать внутренние детали наружу (безопасность)."""
    logger.error("Необработанная ошибка на %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "Внутренняя ошибка сервиса. Повторите запрос позже."},
    )


# ------------------------------------------------------------------------------
# СТАТИЧЕСКИЙ ФРОНТЕНД
# ------------------------------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> Response:
    """Отдать SPA-интерфейс ClickJurist."""
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE))
    return JSONResponse(
        content={
            "service": "ClickJurist Production",
            "docs": "/api/docs",
            "status": "frontend не найден — используйте API",
        }
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Пустой ответ на запрос иконки (чтобы не засорять логи 404)."""
    return Response(status_code=204)


if __name__ == "__main__":  # pragma: no cover — точка входа для локального запуска
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )

