"""Безопасность и анонимизация сессий (152-ФЗ, раздел 4 ТЗ).

Ключевые принципы:
    * приложение НЕ хранит сырые IP-адреса и отпечатки браузера;
    * идентификатор сессии = ``SHA-256(IP + fingerprint + SESSION_HASH_SALT)``;
    * в журналы попадает только анонимизированный UUID сессии
      (UUIDv5 от хеша, необратимый к исходным данным);
    * текст пользовательского запроса никогда не сохраняется в БД.
"""
from __future__ import annotations

import hashlib
import uuid

from fastapi import Request

from backend.config import settings

# Пространство имён для генерации UUID сессий (фиксированное для проекта)
SESSION_NAMESPACE = uuid.UUID("6f1a5d9c-2b7e-4f0a-9c3d-8e5b7a1d4c20")


def client_ip(request: Request) -> str:
    """Извлечь IP клиента с учётом обратного прокси Yandex Cloud.

    Порядок доверия: ``X-Forwarded-For`` → ``X-Real-IP`` → адрес сокета.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "0.0.0.0"


def client_fingerprint(request: Request) -> str:
    """Отпечаток браузера, переданный фронтендом в заголовке.

    На фронтенде отпечаток собирается из стабильных характеристик браузера
    (User-Agent, экран, часовой пояс, canvas-хеш) — см. ``frontend/script.js``.
    """
    fingerprint = request.headers.get("x-client-fingerprint", "").strip()
    if fingerprint:
        return fingerprint
    # Резервный вариант, если заголовок не передан (например, curl)
    return hashlib.sha256(
        (request.headers.get("user-agent", "") + request.headers.get("accept-language", "")).encode(
            "utf-8"
        )
    ).hexdigest()[:32]


def compute_session_hash(ip: str, fingerprint: str) -> str:
    """Построить псевдонимизированный хеш сессии (SHA-256 c солью).

    Соль хранится только в окружении / Yandex KMS, поэтому по значению хеша
    невозможно восстановить IP или отпечаток (необратимая псевдонимизация).
    """
    payload = f"{ip}|{fingerprint}|{settings.SESSION_HASH_SALT}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def anonymized_session_id(session_hash: str) -> str:
    """Превратить хеш сессии в анонимизированный UUID для логов.

    Это единственный идентификатор пользовательской активности, который
    разрешено записывать в Yandex Cloud Logging.
    """
    return str(uuid.uuid5(SESSION_NAMESPACE, session_hash))


def hash_inv_id(value: str) -> str:
    """Необратимо упаковать номер заказа Robokassa для внутреннего хранения."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def session_context(request: Request) -> tuple[str, str, str]:
    """Вернуть тройку ``(session_hash, session_id, ip)`` для обработчиков.

    ``ip`` нужен только для вычисления хеша и не сохраняется.
    """
    ip = client_ip(request)
    fingerprint = client_fingerprint(request)
    session_hash = compute_session_hash(ip, fingerprint)
    return session_hash, anonymized_session_id(session_hash), ip