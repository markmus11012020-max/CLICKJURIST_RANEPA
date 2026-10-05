"""JWT-авторизация сессии (раздел 3.1 ТЗ prompt160926.md)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend import jwt_auth
from backend.config import settings
from backend.db import store
from backend.logging_setup import setup_logging

logger = setup_logging()

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
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


@router.post("/logout")
async def api_auth_logout() -> Response:
    """Удалить JWT-cookie (logout)."""
    response = JSONResponse(content={"message": "JWT-сессия завершена"})
    jwt_auth.clear_session_cookie(response)
    return response


@router.get("/status")
async def api_auth_status(request: Request) -> dict[str, Any]:
    """Проверить валидность текущей JWT-сессии."""
    session_uuid, session_id = jwt_auth.get_session_from_request(request)
    token = request.cookies.get(settings.JWT_COOKIE_NAME, "")
    payload = jwt_auth.decode_session_token(token)
    return {
        "session_uuid": session_uuid,
        "session_id": session_id,
        "authenticated": payload is not None,
        "expires_at": (payload.get("exp") if payload else None),
    }
