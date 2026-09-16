"""JWT-авторизация и управление сессиями (раздел 3.1 ТЗ prompt160926.md).

Ключевые принципы:
    * отказ от привязки сессии к IP-адресу (Wi-Fi → LTE не должен рвать сессию);
    * идентификатор сессии = JWT-токен в HttpOnly, Secure, SameSite=Strict cookie;
    * в БД хранится только ``session_uuid`` (UUIDv4) и технические метрики;
    * персональные данные (ПДн) НЕ сохраняются (No-Data-Retention, 152-ФЗ).

Структура JWT:
    {
        "sub": "<session_uuid>",   # UUIDv4 сессии
        "iat": <issued_at>,        # unix timestamp
        "exp": <expires_at>,       # unix timestamp
        "fp": "<fingerprint_hash>" # хеш отпечатка браузера (для доп. защиты)
    }
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Request, Response

from backend.config import settings

logger = logging.getLogger("clickjurist.jwt_auth")


def _now() -> datetime:
    """Текущее время в UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def fingerprint_hash(fingerprint: str) -> str:
    """Необратимый хеш отпечатка браузера для записи в JWT.

    Используется как дополнительная защита от перехвата токена:
    если злоумышленник украл cookie, но не имеет оригинального
    отпечатка браузера — токен не пройдёт валидацию.
    """
    if not fingerprint:
        return ""
    return hashlib.sha256(
        f"{fingerprint}|{settings.SESSION_HASH_SALT}".encode("utf-8")
    ).hexdigest()[:32]


def create_session_token(
    session_uuid: str | None = None,
    fingerprint: str = "",
) -> tuple[str, str, datetime]:
    """Создать JWT-токен для новой или существующей сессии.

    Args:
        session_uuid: UUIDv4 сессии (если None — генерируется новый).
        fingerprint: отпечаток браузера (хешируется перед записью в JWT).

    Returns:
        Кортеж ``(token, session_uuid, expires_at)``.
    """
    if not session_uuid:
        session_uuid = str(uuid.uuid4())
    issued = _now()
    expires = issued + timedelta(days=settings.JWT_TTL_DAYS)
    payload: dict[str, Any] = {
        "sub": session_uuid,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "fp": fingerprint_hash(fingerprint),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, session_uuid, expires


def decode_session_token(token: str) -> dict[str, Any] | None:
    """Декодировать и валидировать JWT-токен.

    Returns:
        Словарь с полями токена или ``None`` при ошибке валидации.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload if isinstance(payload, dict) else None
    except jwt.ExpiredSignatureError:
        logger.info("JWT: истёк срок действия токена")
        return None
    except jwt.InvalidTokenError as exc:
        logger.info("JWT: невалидный токен (%s)", exc)
        return None


def set_session_cookie(response: Response, token: str, expires: datetime) -> None:
    """Установить HttpOnly cookie с JWT-токеном.

    Флаги:
        * HttpOnly — JS не может прочитать cookie (защита от XSS);
        * Secure — только HTTPS (в production);
        * SameSite=Strict — cookie не отправляется cross-site (защита от CSRF).
    """
    response.set_cookie(
        key=settings.JWT_COOKIE_NAME,
        value=token,
        expires=expires,
        httponly=True,
        secure=settings.JWT_COOKIE_SECURE,
        samesite=settings.JWT_COOKIE_SAMESITE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Удалить cookie сессии (logout)."""
    response.delete_cookie(
        key=settings.JWT_COOKIE_NAME,
        path="/",
        secure=settings.JWT_COOKIE_SECURE,
        samesite=settings.JWT_COOKIE_SAMESITE,
    )


def get_session_from_request(request: Request) -> tuple[str, str]:
    """Извлечь ``(session_uuid, session_id)`` из JWT-cookie.

    Returns:
        Кортеж ``(session_uuid, anonymized_session_id)``.
        Если cookie нет или токен невалиден — генерируется новая сессия.
    """
    token = request.cookies.get(settings.JWT_COOKIE_NAME, "")
    payload = decode_session_token(token)
    if payload and "sub" in payload:
        session_uuid = str(payload["sub"])
        # Анонимизированный session_id для логов (UUIDv5 от session_uuid).
        from backend.security import anonymized_session_id
        return session_uuid, anonymized_session_id(session_uuid)
    # Новая сессия — генерируем UUIDv4.
    new_uuid = str(uuid.uuid4())
    from backend.security import anonymized_session_id
    return new_uuid, anonymized_session_id(new_uuid)


def get_fingerprint_from_request(request: Request) -> str:
    """Извлечь отпечаток браузера из заголовка (для привязки к JWT)."""
    return request.headers.get("x-client-fingerprint", "").strip()