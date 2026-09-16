"""ClickJurist Production — FastAPI-приложение (мегапайплайн юридических услуг).

Архитектура (см. ``prompt.150926.md``):

    Клиент → POST /api/query
              │
              ├─ 0. Платёжный барьер: первый запрос бесплатный, далее 402 + Robokassa
              ├─ 1. STAGE 1 (контур РФ):  YandexGPT / Ollama / regex → маскировка ПДн
              ├─ 2. Web-фактчекинг:       2–3 независимых источника (Yandex/Serper/Tavily)
              ├─ 3. STAGE 2 (внешний):    Gemini 2.5 Flash через AITunnel + failover
              ─ 4. LLM-1 (черновик) → LLM-2 (Senior-асессор) → эталон клиенту
                           │
                           └─ Zero-Storage Logging: только session_id и метрики

Запуск локально:
    python -m backend.main
Запуск в production (Yandex Serverless Containers):
    uvicorn backend.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.config import PROJECT_ROOT, settings
from backend.db import store
from backend.logging_setup import setup_logging
from backend.models import (
    ChecklistRequest,
    ChecklistResponse,
    DocumentRequest,
    DocumentResponse,
    GenerateResponse,
    HealthResponse,
    PaymentCreateRequest,
    PaymentCreateResponse,
    PaymentRequiredResponse,
    QueryRequest,
    SessionStateResponse,
)
from backend.security import session_context
from backend.services import llm_chain, pdf_generator, robokassa
from backend.services.prompts import AI_DISCLAIMER

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
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Session-Id"],
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
    )
    store.log_request(
        session_hash, "consultation", 200, was_free, result.stage2_provider
    )
    response = JSONResponse(content=body.model_dump())
    response.headers["X-Session-Id"] = session_id
    return response


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
        document=llm_chain.attach_disclaimer(document), doc_type=payload.doc_type
    )
    response = JSONResponse(content=body.model_dump())
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
                title="Юридическая консультация ClickJurist",
                content_markup=payload.final_answer,
                subtitle=(
                    f"Документ сформирован ClickJurist • "
                    f"сессия {session_id[:8]}"
                ),
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
                f'attachment; filename="{pdf_generator.filename_for("consultation")}"'
            ),
            "X-Session-Id": session_id,
        },
    )


# ------------------------------------------------------------------------------
# ПЛАТЕЖИ ROBOKASSA (раздел 4 ТЗ)
# ------------------------------------------------------------------------------
@app.post("/api/payment/create", response_model=PaymentCreateResponse)
async def api_payment_create(
    payload: PaymentCreateRequest, request: Request
) -> PaymentCreateResponse:
    """Выставить счёт на оплату выбранной услуги (sandbox: IsTest=1)."""
    session_hash, _, _, _ = session_context(request)
    store.ensure_session(session_hash)
    return PaymentCreateResponse(**robokassa.create_invoice(payload.service, session_hash))  # type: ignore[arg-type]


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
    """Юридическая информация: дисклеймер и режим обработки данных."""
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
        "masking": "",
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

