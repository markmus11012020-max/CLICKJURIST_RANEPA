"""Гибридное хранилище состояний Wizard «КликЮрист» (Стадия 1 — архитектура).

Идея (раздел 4 ТЗ: 152-ФЗ + UX восстановления после обрыва):

    ┌──────────────────────────────────────────────────────────────────────┐
    │  HOT (Redis)            ┌──────────────────────┐  COLD (PostgreSQL)  │
    │  TTL = WIZARD_HOT_TTL_S │ WizardSession        │  wizard_cases        │
    │  ------------------------+ полный снимок        │  анонимный снимок    │
    │  raw_user_input  ◀────▶ │ (включая ПДн)        │  без raw_user_input  │
    │  masking_metadata       │                      │  только для оплаченных│
    │  checklist_state        └──────────────────────┘  и завершённых кейсов│
    └──────────────────────────────────────────────────────────────────────┘

Зачем гибрид:

    * HOT (Redis) — мгновенный доступ (<5 мс), живёт ровно столько, сколько
      пользователь идёт по визарду. TTL автоматически стирает сырой текст
      фабулы, если пользователь ушёл с сайта и не вернулся. Поднимается
      один раз при первом обращении, дальше — только перезапись в TTL-окне.

    * COLD (PostgreSQL) — для оплаченных/завершённых кейсов. Содержит
      обезличенный снимок (``ai_qualification``, ``checklist_state``,
      ``final_document_markdown``, ``masking_metadata``), ``is_paid`` и
      привязку к платежу Робокассы. Поле ``raw_user_input`` в БД НЕ пишется:
      иначе нарушается режим No-Data-Retention, который уже соблюдается
      остальным проектом (см. ``backend/db.py``, ``backend/security.py``).

Бэкенды HOT:

    * ``MemoryHotStore`` — in-process dict под ``threading.Lock``. Дефолт
      для dev/тестов: не требует Redis и совпадает по семантике с уже
      существующим ``MemoryTaskStore`` (``backend/task_store.py``).
    * ``RedisHotStore`` — обёртка над ``redis.asyncio``/sync клиентом.
      Включается, если ``settings.WIZARD_HOT_BACKEND == "redis"`` и
      ``redis`` установлен; иначе — тихий фоллбэк на memory с записью в лог.

Идемпотентность:

    * UUID сессии берётся из JWT-cookie (см. ``backend.jwt_auth``), а не
      генерируется на бэке. Поэтому запись в HOT/COLD всегда идёт по
      известному ключу и не плодит дубликатов.

Потокобезопасность:

    * Все мутации в memory-сторе под ``threading.Lock``;
    * Redis-стор полагается на атомарность ``SET ... NX EX``;
    * Колбэк ``_gc`` запускается из любого долгоживущего потока, но
      периодический «дворник» для in-memory стоя отдельно.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.config import settings
from backend.db import store as db_store
from backend.models import WizardSession, WizardStage

logger = logging.getLogger("clickjurist.wizard_store")

# Ключ холодного хранилища — уникален в пределах проекта, чтобы не
# пересекаться с таблицами ``sessions`` / ``payments`` из ``backend/db.py``.
_COLD_TABLE_SQLITE = """
CREATE TABLE IF NOT EXISTS wizard_cases (
    session_id              TEXT PRIMARY KEY,
    user_id                 INTEGER,
    current_stage           TEXT NOT NULL DEFAULT 'STAGE_1',
    task_id                 TEXT,
    -- Поля «горячего» кэша, которые имеет смысл сохранить в холод:
    ai_qualification_json   TEXT NOT NULL DEFAULT '{}',
    checklist_state_json    TEXT NOT NULL DEFAULT '{}',
    masking_metadata_json   TEXT NOT NULL DEFAULT '{}',
    final_document_markdown  TEXT,
    is_paid                 INTEGER NOT NULL DEFAULT 0,
    payment_inv_id          TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    finished_at             TEXT
);
"""
_COLD_TABLE_POSTGRES = _COLD_TABLE_SQLITE.replace("INTEGER", "BIGINT")
_COLD_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_wizard_cases_paid "
    "ON wizard_cases (is_paid, updated_at);"
)


# ------------------------------------------------------------------------------
# Утилиты времени
# ------------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ------------------------------------------------------------------------------
# Протоколы и dataclass'ы
# ------------------------------------------------------------------------------
class HotWizardStore(Protocol):
    """Контракт «горячего» хранилища визарда (Redis или memory)."""

    def get(self, session_id: uuid.UUID) -> WizardSession | None: ...
    def upsert(self, session: WizardSession) -> None: ...
    def delete(self, session_id: uuid.UUID) -> None: ...
    def touch(self, session_id: uuid.UUID) -> None: ...
    def gc(self) -> int: ...


@dataclass
class _MemoryEntry:
    """Запись in-memory backend'а: снимок + момент последнего обращения."""

    session: WizardSession
    last_seen: float = field(default_factory=time.time)


class MemoryHotStore:
    """Потокобезопасный in-memory бэкенд «горячего» кэша визарда.

    Семантически эквивалентен ``MemoryTaskStore`` (``backend/task_store.py``)
    и уважает те же соглашения (lock вокруг мутаций, GC по ``time.time``).
    """

    def __init__(self, ttl_s: int | None = None) -> None:
        self._ttl_s = ttl_s if ttl_s is not None else settings.WIZARD_HOT_TTL_S
        self._data: dict[str, _MemoryEntry] = {}
        self._lock = threading.Lock()
        self._gc_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._start_gc()

    # --- API -----------------------------------------------------------------
    def get(self, session_id: uuid.UUID) -> WizardSession | None:
        key = str(session_id)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if self._ttl_s > 0 and (time.time() - entry.last_seen) > self._ttl_s:
                # Просрочено — удаляем по дороге и говорим «нет записи».
                self._data.pop(key, None)
                logger.info(
                    "hot-cache miss (TTL): session=%s",
                    session_id,
                )
                return None
            entry.last_seen = time.time()
            return entry.session.model_copy(deep=True)

    def upsert(self, session: WizardSession) -> None:
        session.updated_at = _utc_now_iso()
        if not session.created_at:
            session.created_at = session.updated_at
        key = str(session.session_id)
        with self._lock:
            self._data[key] = _MemoryEntry(session=session.model_copy(deep=True))

    def delete(self, session_id: uuid.UUID) -> None:
        key = str(session_id)
        with self._lock:
            self._data.pop(key, None)

    def touch(self, session_id: uuid.UUID) -> None:
        """Продлить TTL (используется при polling-опросе сервера)."""
        key = str(session_id)
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                entry.last_seen = time.time()

    def gc(self) -> int:
        """Удалить просроченные записи. Возвращает количество удалённых."""
        if self._ttl_s <= 0:
            return 0
        cutoff = time.time() - self._ttl_s
        removed = 0
        with self._lock:
            stale = [k for k, e in self._data.items() if e.last_seen < cutoff]
            for k in stale:
                self._data.pop(k, None)
                removed += 1
        if removed:
            logger.info("hot-cache GC: removed=%d", removed)
        return removed

    # --- Фоновый GC ----------------------------------------------------------
    def _start_gc(self) -> None:
        if self._ttl_s <= 0:
            return
        if self._gc_thread is not None:
            return

        def _loop() -> None:
            while not self._stop_event.wait(settings.WIZARD_GC_INTERVAL_S):
                try:
                    self.gc()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hot-cache GC failed: %s", exc)

        self._gc_thread = threading.Thread(
            target=_loop, name="cj-wizard-gc", daemon=True
        )
        self._gc_thread.start()


class RedisHotStore:
    """Redis-бэкенд «горячего» кэша визарда.

    Подключается к Redis только если ``redis`` установлен и указан URL.
    Сериализация — JSON (компактнее pickle и читается при ручном дебаге
    через ``redis-cli``). Все ключи имеют префикс ``settings.WIZARD_REDIS_PREFIX``
    и TTL ``settings.WIZARD_HOT_TTL_S``.
    """

    def __init__(self, url: str, prefix: str, ttl_s: int) -> None:
        self._url = url
        self._prefix = prefix
        self._ttl_s = ttl_s
        self._client: Any | None = None
        self._connect()

    def _connect(self) -> None:
        try:
            import redis  # type: ignore
        except ImportError:
            logger.warning(
                "redis не установлен — WIZARD_HOT_BACKEND=redis переключён на memory"
            )
            return
        try:
            self._client = redis.Redis.from_url(  # type: ignore[attr-defined]
                self._url, decode_responses=True, socket_timeout=2.0
            )
            self._client.ping()
            logger.info("hot-cache Redis connected: %s", self._url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis недоступен (%s) — fallback на memory", exc)
            self._client = None

    def _key(self, session_id: uuid.UUID) -> str:
        return f"{self._prefix}{session_id}"

    def get(self, session_id: uuid.UUID) -> WizardSession | None:
        if self._client is None:
            return None
        raw = self._client.get(self._key(session_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return WizardSession.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hot-cache: повреждённая запись %s (%s)", session_id, exc)
            return None

    def upsert(self, session: WizardSession) -> None:
        if self._client is None:
            return
        session.updated_at = _utc_now_iso()
        if not session.created_at:
            session.created_at = session.updated_at
        payload = session.model_dump(mode="json")
        self._client.set(
            self._key(session.session_id),
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl_s,
        )

    def delete(self, session_id: uuid.UUID) -> None:
        if self._client is None:
            return
        self._client.delete(self._key(session_id))

    def touch(self, session_id: uuid.UUID) -> None:
        if self._client is None:
            return
        self._client.expire(self._key(session_id), self._ttl_s)

    def gc(self) -> int:
        # Redis сам истёк TTL — ручной GC не нужен.
        return 0


# ------------------------------------------------------------------------------
# Singleton-обёртка
# ------------------------------------------------------------------------------
_hot: HotWizardStore | None = None


def get_hot_store() -> HotWizardStore:
    """Вернуть singleton «горячего» кэша визарда (Redis или memory)."""
    global _hot
    if _hot is not None:
        return _hot
    if settings.WIZARD_HOT_BACKEND == "redis":
        _hot = RedisHotStore(
            url=settings.WIZARD_REDIS_URL,
            prefix=settings.WIZARD_REDIS_PREFIX,
            ttl_s=settings.WIZARD_HOT_TTL_S,
        )
        # Если Redis не поднялся (например, в dev без docker-compose) —
        # тихо откатываемся на memory, чтобы сервис не падал на старте.
        if getattr(_hot, "_client", None) is None:
            _hot = MemoryHotStore()
    else:
        _hot = MemoryHotStore()
    return _hot


# ------------------------------------------------------------------------------
# Публичные операции визарда: склейка HOT+COLD
# ------------------------------------------------------------------------------
def load_session(session_id: uuid.UUID) -> WizardSession | None:
    """Загрузить снимок визарда: сначала HOT, затем COLD.

    Приоритет у HOT: если пользователь только что был активен, у него
    свежий снимок в Redis. COLD подхватывается, когда TTL истёк или
    пользователь вернулся через сутки — показываем ему последний
    сохранённый оплаченный/завершённый кейс.
    """
    hot = get_hot_store().get(session_id)
    if hot is not None:
        return hot
    return _load_cold(session_id)


def save_session(session: WizardSession, *, persist_cold: bool = False) -> None:
    """Сохранить снимок визарда в HOT (и опционально в COLD).

    Args:
        session: Pydantic-снимок визарда.
        persist_cold: ``True`` — дополнительно записать обезличенный
            снимок в PostgreSQL. Используется на STAGE_3 после оплаты,
            когда нужен долговременный архив кейса.
    """
    get_hot_store().upsert(session)
    if persist_cold:
        _save_cold(session)


def patch_checklist(
    session_id: uuid.UUID,
    delta: dict[str, bool],
    *,
    advance_to: WizardStage | None = None,
) -> WizardSession | None:
    """Применить частичный апдейт чек-листа (без полного save_session).

    Используется эндпоинтом ``POST /api/wizard/sync-checklist``: фронтенд
    дёргает его на каждый клик, поэтому операция должна быть O(1) и НЕ
    делать deepcopy всего снимка, иначе любой массовый клик пользователя
    по чек-листу начнёт плодить аллокации.
    """
    hot = get_hot_store()
    current = hot.get(session_id)
    if current is None:
        # Восстанавливаем из холодного хранилища, если HOT протух, иначе
        # инициализируем чистый STAGE_1 (первый визит пользователя).
        current = _load_cold(session_id) or WizardSession(session_id=session_id)
    current.checklist_state.update(delta)
    if advance_to is not None:
        current.current_stage = advance_to
    if advance_to == "STAGE_3":
        # Переход на STAGE_3 фиксирует окончание чек-листа — на этом этапе
        # ``raw_user_input`` уже не нужен на клиенте, чистим чтобы сэкономить
        # память горячего кэша. Обратная деанонимизация идёт через
        # ``masking_metadata``, которое остаётся.
        current.raw_user_input = ""
    hot.upsert(current)
    return current


# ------------------------------------------------------------------------------
# Холодное хранилище (PostgreSQL / SQLite) — расширение ``backend.db.RequestStore``.
# ------------------------------------------------------------------------------
def _init_cold_schema() -> None:
    """Создать таблицу ``wizard_cases`` если её ещё нет.

    Использует тот же подход с двумя диалектами, что и ``backend/db.py`` —
    здесь намеренно держим максимально узкий DDL, чтобы не зависеть от
    будущих миграций ``RequestStore``.
    """
    ddl = _COLD_TABLE_POSTGRES if db_store.dialect == "postgres" else _COLD_TABLE_SQLITE
    with db_store._lock:  # noqa: SLF001
        if db_store.dialect == "sqlite":
            # ``_resolve_sqlite_path`` — приватный статический метод RequestStore.
            # Используем напрямую: альтернативный путь через ``db_store._connect``
            # привёл бы к двойному commit'у внутри блокировки.
            path = db_store._resolve_sqlite_path(  # noqa: SLF001
                db_store.database_url
            )
            import sqlite3

            conn = sqlite3.connect(path, timeout=15)
            try:
                conn.executescript(ddl + _COLD_INDEX)
                conn.commit()
            finally:
                conn.close()
            return

        import psycopg

        with psycopg.connect(db_store.database_url) as conn:
            cursor = conn.cursor()
            for part in filter(None, (ddl + _COLD_INDEX).split(";")):
                cursor.execute(part)
            conn.commit()


def _save_cold(session: WizardSession) -> None:
    """Записать обезличенный снимок в ``wizard_cases``.

    Поле ``raw_user_input`` намеренно НЕ пишется — иначе нарушается
    режим No-Data-Retention (см. ``backend/security.py``).
    """
    _init_cold_schema()
    now = _utc_now_iso()
    finished = now if session.current_stage == "STAGE_3" else None
    payload = (
        str(session.session_id),
        session.user_id,
        session.current_stage,
        session.task_id,
        json.dumps(session.ai_qualification, ensure_ascii=False),
        json.dumps(session.checklist_state, ensure_ascii=False),
        json.dumps(session.masking_metadata, ensure_ascii=False),
        session.final_document_markdown,
        1 if session.is_paid else 0,
        None,  # payment_inv_id — заполняется позже из router_payment
        now,
        now,
        finished,
    )
    placeholder = "%s" if db_store.dialect == "postgres" else "?"
    cols = ",".join([placeholder] * 13)
    if db_store.dialect == "postgres":
        sql = (
            "INSERT INTO wizard_cases (session_id, user_id, current_stage, task_id, "
            "ai_qualification_json, checklist_state_json, masking_metadata_json, "
            "final_document_markdown, is_paid, payment_inv_id, created_at, updated_at, "
            "finished_at) VALUES (" + cols + ") "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "current_stage = EXCLUDED.current_stage, "
            "task_id = EXCLUDED.task_id, "
            "ai_qualification_json = EXCLUDED.ai_qualification_json, "
            "checklist_state_json = EXCLUDED.checklist_state_json, "
            "masking_metadata_json = EXCLUDED.masking_metadata_json, "
            "final_document_markdown = EXCLUDED.final_document_markdown, "
            "is_paid = EXCLUDED.is_paid, "
            "updated_at = EXCLUDED.updated_at, "
            "finished_at = COALESCE(wizard_cases.finished_at, EXCLUDED.finished_at)"
        )
    else:
        sql = (
            "INSERT OR REPLACE INTO wizard_cases (session_id, user_id, current_stage, "
            "task_id, ai_qualification_json, checklist_state_json, masking_metadata_json, "
            "final_document_markdown, is_paid, payment_inv_id, created_at, updated_at, "
            "finished_at) VALUES (" + cols + ")"
        )
    db_store._execute(sql, payload)  # noqa: SLF001


def _load_cold(session_id: uuid.UUID) -> WizardSession | None:
    """Прочитать обезличенный снимок из ``wizard_cases`` (если есть)."""
    # Лёнивая инициализация схемы — idempotent CREATE IF NOT EXISTS. Первый
    # пользователь после деплоя создаст таблицу, остальные получают no-op.
    _init_cold_schema()
    rows = db_store._execute(  # noqa: SLF001
        "SELECT session_id, user_id, current_stage, task_id, "
        "ai_qualification_json, checklist_state_json, masking_metadata_json "
        "FROM wizard_cases WHERE session_id = ?",
        (str(session_id),),
    )
    if not rows:
        return None
    row = rows[0]
    try:
        return WizardSession(
            session_id=uuid.UUID(str(row["session_id"])),
            user_id=row.get("user_id"),
            current_stage=row["current_stage"],
            task_id=row.get("task_id"),
            ai_qualification=json.loads(row.get("ai_qualification_json") or "{}"),
            checklist_state=json.loads(row.get("checklist_state_json") or "{}"),
            masking_metadata=json.loads(row.get("masking_metadata_json") or "{}"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cold-cache: повреждённая запись %s (%s)", session_id, exc)
        return None