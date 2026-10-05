"""Точка входа ClickJurist Production: сборка FastAPI-приложения.

Модуль сознательно тонкий. Всё, что относится к отдельным доменам,
разнесено по роутерам пакета :mod:`backend.api`:

* :mod:`backend.api.router_query` — консультация и фоновые задачи;
* :mod:`backend.api.router_docs` — чек-лист, документ, PDF, пакет;
* :mod:`backend.api.router_payment` — оплата Robokassa;
* :mod:`backend.api.router_auth` — JWT-сессия;
* :mod:`backend.api.router_meta` — сессия, тарифы, здоровье, SPA;
* :mod:`backend.api.deps` — платёжный барьер.

Здесь остаются только вещи, относящиеся ко всему приложению сразу:
создание ``FastAPI``, middleware, обработчики ошибок, монтирование
статики и подключение модуля чат-бота.

Запуск локально::

    python -m backend.main
    # или
    uvicorn backend.main:app --reload
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import (
    router_auth,
    router_docs,
    router_meta,
    router_payment,
    router_query,
    router_wizard,
)
from backend.config import PROJECT_ROOT, settings
from backend.logging_setup import setup_logging
from backend.services.rate_limit import init_limiters
from backend.startup_checks import validate_startup
from backend.static_files import NoCacheStaticFiles

# --- Инициализация окружения --------------------------------------------------
logger = setup_logging()

FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Поднимаем in-memory счётчики rate-limit. Делать это ДО создания приложения,
# чтобы любой обработчик, использующий ``session_limiter`` / ``ip_limiter``,
# получил не-None singleton'ы.
init_limiters(
    session_per_hour=settings.RATE_LIMIT_SESSION_PER_HOUR,
    ip_per_min=settings.RATE_LIMIT_IP_PER_MIN,
)

# Критичная конфигурация (обход оплаты, секреты по умолчанию) не должна
# молча проходить в production — проверяем до создания приложения.
validate_startup()

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
    # credentials включаются только когда Origin перечислен явно:
    # со звёздочкой браузер всё равно их не пропустит.
    allow_credentials=settings.cors_credentials,
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
# ОБРАБОТЧИКИ ОШИБОК
# ------------------------------------------------------------------------------
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
# ПОДКЛЮЧЕНИЕ РОУТЕРОВ
# Порядок важен: /api/query/stream и /api/query/status должны быть
# зарегистрированы раньше, чем общий префикс, хотя FastAPI и разводит
# их по приоритету сам — порядок оставлен по доменам для читаемости.
# ------------------------------------------------------------------------------
app.include_router(router_query.router)
app.include_router(router_docs.router)
app.include_router(router_payment.router)
app.include_router(router_auth.router)
app.include_router(router_meta.router)
app.include_router(router_wizard.router)


# ------------------------------------------------------------------------------
# СТАТИЧЕСКИЙ ФРОНТЕНД
# ------------------------------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount(
        "/static",
        NoCacheStaticFiles(directory=str(FRONTEND_DIR)),
        name="static",
    )


# ------------------------------------------------------------------------------
# МОДУЛЬ ВСПЛЫВАЮЩЕГО ЧАТ-БОТА
# Модуль опционален: при ошибке импорта сайт продолжает работать без него.
# ------------------------------------------------------------------------------
try:
    from chatbot.app import register as register_chatbot

    register_chatbot(app)
except Exception as _chatbot_exc:  # pragma: no cover — модуль не критичен
    logger.warning("Чат-бот не подключён: %s", _chatbot_exc)


# ------------------------------------------------------------------------------
# ОБРАТНАЯ СОВМЕСТИМОСТЬ
# Логика переехала в backend/api/*, но прежние имена из `backend.main`
# импортируются тестами. Алиасы оставлены, чтобы не ломать их контракт
# и не заставлять править внешние импорты.
# ------------------------------------------------------------------------------
from backend.api.deps import payment_required as _payment_required  # noqa: E402
from backend.api.deps import session_gate as _session_gate  # noqa: E402

__all__ = ["app", "_session_gate", "_payment_required"]


if __name__ == "__main__":  # pragma: no cover — точка входа для локального запуска
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )
