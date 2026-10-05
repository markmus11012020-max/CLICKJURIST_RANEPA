"""Общие зависимости HTTP-слоя: платёжный барьер (раздел 4 ТЗ) + rate-limit.

Вынесено из ``backend/main.py``, чтобы правило оплаты и анти-абьюз были в
одном месте и не дублировались в каждом роутере.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.db import store
from backend.logging_setup import setup_logging
from backend.models import PaymentRequiredResponse
from backend.security import anonymized_session_id, quota_keys, session_context
from backend.services import robokassa
from backend.services.rate_limit import (
    get_client_ip,
    ip_limiter,
    session_limiter,
)

logger = setup_logging()


def payment_required(service: str, session_hash: str) -> JSONResponse:
    """Вернуть 402 Payment Required с готовой ссылкой на оплату Robokassa."""
    invoice = robokassa.create_invoice(service, session_hash)
    payload = PaymentRequiredResponse(
        detail=(
            "Сервис работает по предоплате. Оформите платёж, "
            "чтобы получить ответ."
        ),
        service=service,  # type: ignore[arg-type]
        amount=int(invoice["amount"]),
        payment_url=str(invoice["payment_url"]),
        inv_id=str(invoice["inv_id"]),
        is_test=bool(invoice["is_test"]),
    )
    return JSONResponse(status_code=402, content=payload.model_dump())


# ---------------------------------------------------------------------------
# Rate-limit: 429 Too Many Requests с заголовком Retry-After
# ---------------------------------------------------------------------------
def enforce_ip_rate_limit(request: Request) -> None:
    """Проверить лимит по IP. Вызывать ПЕРВЫМ в каждом дорогом эндпоинте —
    до парсинга тела и обращения к БД, чтобы отбить ботнет дёшево.

    Raises:
        HTTPException: 429 + ``Retry-After`` в заголовке, если лимит исчерпан.
    """
    if ip_limiter is None:  # защита от тестового импорта без init
        return
    client_ip = get_client_ip(request)
    allowed, retry = ip_limiter.check(f"ip:{client_ip}")
    if not allowed:
        logger.info(
            "Rate-limit hit (ip): client_ip=%s retry_after=%ss",
            client_ip,
            retry,
        )
        # Поднимаем исключение, чтобы FastAPI сам превратил в ответ с
        # заголовком Retry-After. Body с подробностями — для отладки.
        from fastapi import HTTPException

        raise HTTPException(
            status_code=429,
            detail=(
                f"Слишком много запросов с вашего IP. Попробуйте через "
                f"{retry} сек."
            ),
            headers={"Retry-After": str(retry)},
        )


def enforce_session_rate_limit(session_hash: str) -> None:
    """Проверить лимит по сессии. Вызывать с ``session_hash``, уже
    извлечённым через ``session_context``, — так не делаем парсинг заголовков
    дважды.

    Raises:
        HTTPException: 429 + ``Retry-After``, если лимит исчерпан.
    """
    if session_limiter is None or not session_hash:
        return  # анонимный вызов — лимит по IP уже отработал
    allowed, retry = session_limiter.check(f"session:{session_hash}")
    if not allowed:
        logger.info(
            "Rate-limit hit (session): session=%s retry_after=%ss",
            anonymized_session_id(session_hash),
            retry,
        )
        from fastapi import HTTPException

        raise HTTPException(
            status_code=429,
            detail=(
                f"Слишком много запросов из этой сессии. Попробуйте через "
                f"{retry} сек."
            ),
            headers={"Retry-After": str(retry)},
        )


def session_gate(
    request: Request, service: str
) -> tuple[str, str, bool, JSONResponse | None]:
    """Проверить право на запрос: бесплатный доступ или оплаченный период.

    Returns:
        ``(session_hash, session_id, was_free, denial)``. Если ``denial`` не
        ``None`` — запрос нужно прервать ответом 402.

    Notes:
        Бесплатный лимит проверяется по ДВУМ независимым ключам — по сессии
        браузера и по сети (IP). Отпечаток браузера приходит из клиентского
        заголовка и может быть подменён, поэтому одного браузерного ключа
        недостаточно: смена отпечатка или очистка хранилища не должна выдавать
        новый бесплатный запрос. Сетевой счётчик списывается здесь же, в момент
        выдачи доступа, — эндпоинту остаётся списать только браузерный ключ.

        Оплаченный доступ не зависит ни от одного из ключей и проверяется
        первым: заплативший пользователь не блокируется из-за «соседей» по IP.

        Если включён режим разработчика (``settings.DEV_BYPASS_PAYWALL=True``),
        платёжный барьер полностью отключается: 402 никогда не возвращается,
        бесплатный лимит не списывается, сессия регистрируется в БД только
        ради совместимости со ``store.log_request()``.

        В коде обход выключен по умолчанию: его нужно включать явно
        (``DEV_BYPASS_PAYWALL=true``) — иначе забытая переменная окружения
        молча отключала бы оплату в production.
    """
    session_hash, session_id, _ = session_context(request)
    store.ensure_session(session_hash)

    # --- Anti-abuse: rate-limit --------------------------------------------
    # Срабатывает ДО free-trial и оплаты: даже платный пользователь не должен
    # иметь возможность долбить API скриптом. IP-лимит дешёвый (нет обращения
    # к БД) и отбивает ботнет; session-лимит страхует от угона session_hash.
    # При ``DEV_BYPASS_PAYWALL=True`` лимиты тоже отключаются — это сделано
    # намеренно, чтобы локальный dev-режим не упирался в 429.
    if not settings.DEV_BYPASS_PAYWALL:
        enforce_ip_rate_limit(request)
        enforce_session_rate_limit(session_hash)

    # --- Dev Mode: обход платёжного барьера ----------------------------------
    # Проверяется ПЕРВЫМ делом — до has_paid_access / can_use_free_request,
    # чтобы 402 гарантированно никогда не вернулся из этой функции.
    if settings.DEV_BYPASS_PAYWALL:
        logger.info(
            "Dev Mode: Paywall bypassed (session=%s, service=%s)",
            anonymized_session_id(session_hash),
            service,
        )
        return session_hash, session_id, False, None

    if store.has_paid_access(session_hash):
        return session_hash, session_id, False, None

    # Сессионный ключ и сетевой ключ — два независимых ограничителя.
    _, network_hash = quota_keys(request)
    if not store.can_use_free_request(session_hash):
        return session_hash, session_id, False, payment_required(service, session_hash)
    if not store.can_use_free_request_by_ip(network_hash):
        logger.info(
            "Free tier exhausted for network (session=%s, service=%s)",
            anonymized_session_id(session_hash),
            service,
        )
        return session_hash, session_id, False, payment_required(service, session_hash)

    store.consume_network_free_request(network_hash)
    return session_hash, session_id, True, None
