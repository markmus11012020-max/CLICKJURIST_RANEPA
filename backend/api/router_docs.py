"""Чек-лист, шаблон документа, PDF и пакетный тариф (Шаги 2–3 ТЗ)."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from backend import task_store
from backend.api.deps import session_gate
from backend.config import settings
from backend.db import store
from backend.logging_setup import setup_logging
from backend.models import (
    AsyncTaskResponse,
    ChecklistRequest,
    ChecklistResponse,
    DocumentRequest,
    DocumentResponse,
    PackageRequest,
    PackageResponse,
)
from backend.services import llm_chain, pdf_generator
from backend.services.prompts import with_dynamic_disclaimer

logger = setup_logging()

router = APIRouter(prefix="/api", tags=["documents"])

#: Таймаут сборки PDF, секунды. Реальный отчёт собирается за 5–10 с;
#: всё, что дольше, — зависание, и клиент должен получить 504, а не
#: бесконечное соединение.
PDF_TIMEOUT_S = 60.0

#: Размер порции при эмите токенов в фоновой задаче.
_TOKEN_CHUNK = 120


# ------------------------------------------------------------------------------
# Чек-лист и документ
# ------------------------------------------------------------------------------
@router.post("/checklist", response_model=ChecklistResponse)
async def api_checklist(payload: ChecklistRequest, request: Request) -> Response:
    """Пошаговый чек-лист действий по готовой консультации."""
    session_hash, session_id, was_free, denial = session_gate(request, "checklist")
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


@router.post("/document", response_model=DocumentResponse)
async def api_document(payload: DocumentRequest, request: Request) -> Response:
    """Черновик процессуального документа (иск / претензия / жалоба)."""
    session_hash, session_id, was_free, denial = session_gate(request, "document")
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
# Асинхронная генерация чек-листа и документа (Шаги 2/3 ТЗ prompt170926.md)
# ------------------------------------------------------------------------------
def _publish_progress(record: task_store.TaskRecord, event: dict) -> None:
    """Пробросить событие в ленту задачи и обновить прогресс для polling."""
    record.push_event(event)
    if event.get("type") == "progress":
        task_store.get_task_store().update(
            record.task_id,
            stage=event.get("stage", ""),
            progress=event.get("progress", 0),
        )


def _emit_tokens(record: task_store.TaskRecord, text: str) -> None:
    """Эмитить текст порциями — эффект «живой печати» в ленте задачи."""
    for i in range(0, len(text), _TOKEN_CHUNK):
        record.push_event({"type": "token", "text": text[i:i + _TOKEN_CHUNK]})


def _run_checklist_task(
    record: task_store.TaskRecord, query: str, final_answer: str
) -> dict:
    """Фоновая задача генерации чек-листа со стримингом токенов."""
    try:
        from backend.services.pii_masker import mask_query

        _publish_progress(
            record, {"type": "progress", "stage": "masking", "progress": 10}
        )
        mask = mask_query(query)
        _publish_progress(
            record, {"type": "progress", "stage": "masking_done", "progress": 25}
        )

        _publish_progress(
            record, {"type": "progress", "stage": "drafting", "progress": 40}
        )
        text = llm_chain.draft_checklist(mask.masked_query, final_answer)
        _emit_tokens(record, text)

        _publish_progress(record, {"type": "progress", "stage": "done", "progress": 100})
        return {"checklist": llm_chain.attach_disclaimer(text)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Фоновая задача чек-листа упала")
        return {"error": f"{type(exc).__name__}: {exc}"}


#: Признаки корпоративного спора, для которого административный порядок
#: (подача жалобы) законодательством РФ не предусмотрен.
_CORPORATE_MARKERS = (
    "ооо",
    "генеральный директор",
    "акции",
    "доля 15%",
    "крупная сделка",
)

_CORPORATE_REDIRECT_NOTICE = (
    "ℹ️ Для вашего случая жалоба не подойдёт — закон предлагает другой путь. "
    "Мы автоматически подготовили исковое заявление в арбитражный суд: его "
    "можно сразу подписать и подать.\n\n"
)


def _run_document_task(
    record: task_store.TaskRecord, query: str, doc_type: str
) -> dict:
    """Фоновая задача генерации документа со стримингом токенов."""
    try:
        from backend.services.pii_masker import mask_query

        _publish_progress(
            record, {"type": "progress", "stage": "masking", "progress": 10}
        )
        mask = mask_query(query)
        _publish_progress(
            record, {"type": "progress", "stage": "masking_done", "progress": 25}
        )

        _publish_progress(
            record, {"type": "progress", "stage": "drafting", "progress": 40}
        )

        # Жалоба по корпоративному спору недопустима — подменяем на иск.
        if doc_type == "complaint" and any(
            marker in (query or "").lower() for marker in _CORPORATE_MARKERS
        ):
            doc_type = "lawsuit"
            record.push_event({"type": "token", "text": _CORPORATE_REDIRECT_NOTICE})

        text = llm_chain.draft_document(mask.masked_query, doc_type)
        _emit_tokens(record, text)

        _publish_progress(record, {"type": "progress", "stage": "done", "progress": 100})
        return {
            "document": llm_chain.attach_disclaimer(text, for_document=True),
            "doc_type": doc_type,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Фоновая задача документа упала")
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.post("/checklist/async", response_model=AsyncTaskResponse, status_code=202)
async def api_checklist_async(
    payload: ChecklistRequest, request: Request
) -> Response:
    """Поставить генерацию чек-листа в фоновую очередь (Шаг 2 ТЗ prompt170926.md)."""
    session_hash, session_id, was_free, denial = session_gate(request, "checklist")
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


@router.post("/document/async", response_model=AsyncTaskResponse, status_code=202)
async def api_document_async(
    payload: DocumentRequest, request: Request
) -> Response:
    """Поставить генерацию документа в фоновую очередь (Шаг 3 ТЗ prompt170926.md)."""
    session_hash, session_id, was_free, denial = session_gate(request, "document")
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


# ------------------------------------------------------------------------------
# PDF
# ------------------------------------------------------------------------------
@router.post("/pdf")
async def api_pdf(payload: ChecklistRequest, request: Request) -> Response:
    """PDF-версия консультации с поддержкой кириллицы (ReportLab).

    Тяжёлая CPU-bound сборка PDF выполняется в отдельном потоке через
    ``run_in_threadpool``, чтобы не блокировать event-loop FastAPI.
    ``asyncio.wait_for`` гарантирует, что зависший генератор не держит
    соединение открытым бесконечно — клиент получит HTTP 504 по таймауту.
    """
    session_hash, _session_id, was_free, denial = session_gate(request, "pdf")
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

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
# Пакетный тариф «Решение проблемы под ключ» (раздел 6 ТЗ prompt160926.md)
# ------------------------------------------------------------------------------
@router.post("/package", response_model=PackageResponse)
async def api_package(payload: PackageRequest, request: Request) -> Response:
    """Пакетная генерация: консультация + чек-лист + документ (раздел 6 ТЗ).

    Единый тариф ``basic`` (195 ₽) — консультация + чек-лист + шаблон документа.

    Доступ ТОЛЬКО после оплаты (проверяется через ``session_gate``).
    """
    service_code = "package_basic"
    session_hash, session_id, was_free, denial = session_gate(request, service_code)  # type: ignore[arg-type]
    if denial is not None:
        return denial
    if was_free:
        store.consume_free_request(session_hash)
    store.register_request(session_hash, was_free)

    body = PackageResponse(amount=settings.PRICE_PACKAGE_BASIC)

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
