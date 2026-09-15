"""Хранилище данных ClickJurist Production.

Назначение (раздел 4 ТЗ): отслеживать входящие запросы по **хешированному**
IP + отпечатку браузера, хранить признак ``is_free`` и статусы оплат Robokassa.

Особенности:
    * development → встроенный ``sqlite3`` (файл ``data/clickjurist.db``);
    * production → ``PostgreSQL`` (Yandex Managed Service for PostgreSQL);
    * в БД НЕ сохраняется текст запроса, ФИО, адреса и иные ПДн —
      только псевдонимизированный ``session_hash`` и технические метрики.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from backend.config import settings

DIALECT_SQLITE = "sqlite"
DIALECT_POSTGRES = "postgres"

_SCHEMA_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_hash        TEXT PRIMARY KEY,
    is_free             INTEGER NOT NULL DEFAULT 1,
    free_requests_used  INTEGER NOT NULL DEFAULT 0,
    requests_total      INTEGER NOT NULL DEFAULT 0,
    paid_until          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""

_SCHEMA_PAYMENTS = """
CREATE TABLE IF NOT EXISTS payments (
    inv_id       TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    service      TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    paid_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_session ON payments (session_hash);
"""

_SCHEMA_REQUEST_LOG_SQLITE = """
CREATE TABLE IF NOT EXISTS request_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_hash TEXT NOT NULL,
    service      TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    is_free      INTEGER NOT NULL DEFAULT 0,
    provider     TEXT,
    latency_ms   INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_log_session ON request_log (session_hash);
"""

_SCHEMA_REQUEST_LOG_POSTGRES = _SCHEMA_REQUEST_LOG_SQLITE.replace(
    "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"
)


def utc_now_iso() -> str:
    """Текущее время в ISO-8601 (UTC) — единый формат для обоих диалектов."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def iso_in_days(days: int) -> str:
    """Метка времени в будущем (используется для ``paid_until``)."""
    moment = datetime.now(timezone.utc) + timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


class RequestStore:
    """Слой доступа к данным: сессии, платежи и технический журнал запросов."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or settings.DATABASE_URL
        self.dialect = (
            DIALECT_SQLITE
            if self.database_url.startswith("sqlite")
            else DIALECT_POSTGRES
        )
        self._lock = threading.Lock()
        self._sqlite_path: str | None = None
        if self.dialect == DIALECT_SQLITE:
            self._sqlite_path = self._resolve_sqlite_path(self.database_url)
            Path(self._sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # --- Соединения ---------------------------------------------------------
    @staticmethod
    def _resolve_sqlite_path(database_url: str) -> str:
        """Преобразовать ``sqlite:///./data/x.db`` в абсолютный путь."""
        raw = database_url.split("sqlite:///", 1)[-1]
        if raw.startswith("./"):
            raw = raw[2:]
        path = Path(raw)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        return str(path)

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        """Открыть соединение с БД (SQLite или PostgreSQL)."""
        if self.dialect == DIALECT_SQLITE:
            conn = sqlite3.connect(self._sqlite_path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
            return

        import psycopg  # импорт по требованию — нужен только в production

        conn = psycopg.connect(self.database_url)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Выполнить SQL-запрос и вернуть строки как словари."""
        if self.dialect != DIALECT_SQLITE:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows: list[dict[str, Any]] = []
            if cursor.description:
                columns = [col[0] for col in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.close()
            return rows

    def init_schema(self) -> None:
        """Создать таблицы, если их ещё нет."""
        statements = [
            _SCHEMA_SESSIONS,
            _SCHEMA_PAYMENTS,
            (
                _SCHEMA_REQUEST_LOG_SQLITE
                if self.dialect == DIALECT_SQLITE
                else _SCHEMA_REQUEST_LOG_POSTGRES
            ),
        ]
        with self._lock:
            if self.dialect == DIALECT_SQLITE:
                conn = sqlite3.connect(self._sqlite_path, timeout=15)
                try:
                    for statement in statements:
                        conn.executescript(statement)
                    conn.commit()
                finally:
                    conn.close()
                return

            import psycopg

            with psycopg.connect(self.database_url) as conn:
                cursor = conn.cursor()
                for statement in statements:
                    for part in filter(None, statement.split(";")):
                        cursor.execute(part)
                cursor.close()

    # --- Сессии -------------------------------------------------------------
    def get_session(self, session_hash: str) -> dict[str, Any] | None:
        """Вернуть запись сессии или ``None``, если сессия ещё не создана."""
        rows = self._execute(
            "SELECT * FROM sessions WHERE session_hash = ?", (session_hash,)
        )
        return rows[0] if rows else None

    def ensure_session(self, session_hash: str) -> dict[str, Any]:
        """Получить сессию, создав её при первом обращении (``is_free = 1``)."""
        session = self.get_session(session_hash)
        if session:
            return session

        now = utc_now_iso()
        with self._lock:
            self._execute(
                "INSERT INTO sessions (session_hash, is_free, free_requests_used, "
                "requests_total, paid_until, created_at, updated_at) "
                "VALUES (?, 1, 0, 0, NULL, ?, ?)",
                (session_hash, now, now),
            )
        created = self.get_session(session_hash)
        return created if created else {}

    def can_use_free_request(self, session_hash: str) -> bool:
        """True, если у сессии ещё есть неиспользованный бесплатный запрос."""
        session = self.ensure_session(session_hash)
        if not int(session.get("is_free", 0)):
            return False
        return int(session.get("free_requests_used", 0)) < settings.FREE_TIER_REQUESTS

    def consume_free_request(self, session_hash: str) -> None:
        """Списать бесплатный запрос и снять флаг ``is_free``.

        Платёжный барьер (раздел 4 ТЗ): первый запрос бесплатный, последующие
        требуют оплаты — состояние сессии меняется на ``is_free = False``.
        """
        session = self.ensure_session(session_hash)
        used = int(session.get("free_requests_used", 0)) + 1
        still_free = 1 if used < settings.FREE_TIER_REQUESTS else 0
        self._execute(
            "UPDATE sessions SET is_free = ?, free_requests_used = ?, updated_at = ? "
            "WHERE session_hash = ?",
            (still_free, used, utc_now_iso(), session_hash),
        )

    def register_request(self, session_hash: str, was_free: bool) -> None:
        """Увеличить счётчик обращений сессии."""
        self._execute(
            "UPDATE sessions SET requests_total = requests_total + 1, updated_at = ? "
            "WHERE session_hash = ?",
            (utc_now_iso(), session_hash),
        )

    # --- Платный доступ -----------------------------------------------------
    def has_paid_access(self, session_hash: str) -> bool:
        """True, если у сессии есть действующий оплаченный доступ."""
        session = self.get_session(session_hash)
        if not session:
            return False
        paid_until = session.get("paid_until")
        return bool(paid_until) and str(paid_until) > utc_now_iso()

    def grant_paid_access(self, session_hash: str, days: int = 30) -> None:
        """Выдать оплаченный доступ на ``days`` дней после успешного платежа."""
        self.ensure_session(session_hash)
        self._execute(
            "UPDATE sessions SET is_free = 0, paid_until = ?, updated_at = ? "
            "WHERE session_hash = ?",
            (iso_in_days(days), utc_now_iso(), session_hash),
        )

    # --- Платежи ------------------------------------------------------------
    def create_payment(
        self, inv_id: str, session_hash: str, service: str, amount: int
    ) -> None:
        """Зафиксировать выставленный счёт Robokassa со статусом ``pending``."""
        self._execute(
            "INSERT INTO payments (inv_id, session_hash, service, amount, status, "
            "created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (inv_id, session_hash, service, amount, utc_now_iso()),
        )

    def get_payment(self, inv_id: str) -> dict[str, Any] | None:
        """Вернуть платёж по номеру счёта."""
        rows = self._execute("SELECT * FROM payments WHERE inv_id = ?", (inv_id,))
        return rows[0] if rows else None

    def mark_payment_paid(self, inv_id: str) -> dict[str, Any] | None:
        """Отметить счёт оплаченным и вернуть запись платежа."""
        self._execute(
            "UPDATE payments SET status = 'paid', paid_at = ? WHERE inv_id = ?",
            (utc_now_iso(), inv_id),
        )
        return self.get_payment(inv_id)

    def mark_payment_failed(self, inv_id: str) -> None:
        """Отметить счёт неуспешным (отказ или ошибка оплаты)."""
        self._execute(
            "UPDATE payments SET status = 'failed' WHERE inv_id = ?", (inv_id,)
        )

    # --- Технический журнал (без ПДн) ---------------------------------------
    def log_request(
        self,
        session_hash: str,
        service: str,
        status_code: int,
        is_free: bool = False,
        provider: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """Записать техническую метрику запроса.

        ВАЖНО: ни текст запроса, ни ответ модели здесь не сохраняются —
        только обезличенные метрики (152-ФЗ, режим No-Data-Retention).
        """
        self._execute(
            "INSERT INTO request_log (session_hash, service, status_code, is_free, "
            "provider, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_hash,
                service,
                status_code,
                1 if is_free else 0,
                provider,
                latency_ms,
                utc_now_iso(),
            ),
        )

    def stats(self) -> dict[str, int]:
        """Сводная анонимная статистика для служебного эндпоинта."""
        sessions = self._execute("SELECT COUNT(*) AS c FROM sessions")[0]["c"]
        payments = self._execute("SELECT COUNT(*) AS c FROM payments")[0]["c"]
        paid = self._execute(
            "SELECT COUNT(*) AS c FROM payments WHERE status = 'paid'"
        )[0]["c"]
        requests_total = self._execute("SELECT COUNT(*) AS c FROM request_log")[0]["c"]
        return {
            "sessions": int(sessions),
            "payments": int(payments),
            "payments_paid": int(paid),
            "requests_total": int(requests_total),
        }


# Глобальный экземпляр хранилища для приложения
store = RequestStore()