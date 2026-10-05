"""Идентификация сессии чат-бота.

Виджет генерирует случайный идентификатор и хранит его в
``localStorage``. На сервере он используется **только** как ключ, и
перед этим хэшируется с солью проекта — в логах и в памяти не остаётся
сырых значений, согласованно с остальным приложением (152-ФЗ).
"""

from __future__ import annotations

import hashlib
import hmac

from backend.config import settings


class SessionIdentity:
    """Хэширование клиентского идентификатора сессии.

    Сравнение — постоянное время (``hmac.compare_digest``), чтобы по
    времени ответа нельзя было подбирать идентификаторы.
    """

    def __init__(self, salt: str | None = None) -> None:
        self._salt = (salt if salt is not None else settings.SESSION_HASH_SALT).encode("utf-8")

    def derive(self, client_session_id: str) -> str:
        """Получить псевдонимизированный ключ сессии."""
        digest = hmac.new(self._salt, client_session_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest

    @staticmethod
    def is_valid(client_session_id: str | None) -> bool:
        """Проверить формат клиентского идентификатора.

        Ожидается 8–128 символов из букв, цифр, дефиса и подчёркивания —
        этого достаточно для UUID и достаточно строго, чтобы отсечь
        мусор и попытки инъекции в ключ хранилища.
        """
        if not client_session_id or not (8 <= len(client_session_id) <= 128):
            return False
        return all(ch.isalnum() or ch in "-_" for ch in client_session_id)

    @staticmethod
    def matches(left: str, right: str) -> bool:
        """Сравнить два идентификатора за постоянное время."""
        return hmac.compare_digest(left, right)
