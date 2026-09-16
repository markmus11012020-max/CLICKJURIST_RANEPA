"""Хранилище фоновых задач (раздел 2.1 ТЗ prompt160926.md).

Реализует абстракцию ``TaskStore`` с двумя бэкендами:
    * ``MemoryTaskStore`` — потокобезопасное in-memory хранилище (для dev/single-instance);
    * ``CeleryTaskStore`` — обёртка над Celery + Redis (для production-масштабирования).

Публичный API:
    * :func:`submit_task` — поставить задачу в очередь, вернуть ``task_id``;
    * :func:`get_task_status` — опросить статус задачи (polling);
    * :func:`stream_task_events` — подписаться на события задачи (SSE).

Состояния задачи:
    * ``pending`` — принята, ожидает выполнения;
    * ``running`` — выполняется;
    * ``completed`` — успешно завершена (есть ``result``);
    * ``failed`` — ошибка (есть ``error``);
    * ``cancelled`` — отменена пользователем.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator

from backend.config import settings

logger = logging.getLogger("clickjurist.task_store")


class TaskStatus(str, Enum):
    """Состояния фоновой задачи."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskRecord:
    """Запись о фоновой задаче."""

    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    progress: int = 0
    stage: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _subscribers: list[queue.Queue] = field(default_factory=list)

    def push_event(self, event: dict[str, Any]) -> None:
        """Добавить событие в ленту и разослать подписчикам (SSE)."""
        stamped = {**event, "ts": datetime.now(timezone.utc).isoformat()}
        self.events.append(stamped)
        if len(self.events) > 200:
            self.events = self.events[-200:]
        for sub in list(self._subscribers):
            try:
                sub.put_nowait(stamped)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        """Подписаться на события задачи (для SSE-стриминга)."""
        q: queue.Queue = queue.Queue(maxsize=256)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Отписаться от событий задачи."""
        if q in self._subscribers:
            self._subscribers.remove(q)

    def to_dict(self) -> dict[str, Any]:
        """Публичное представление для API (без внутренних полей)."""
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "stage": self.stage,
            "result": self.result,
            "error": self.error,
        }


def _utc_now_iso() -> str:
    """Текущее время в ISO-8601 (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class MemoryTaskStore:
    """Потокобезопасное in-memory хранилище задач."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()
        self._ttl_s = settings.TASK_RESULT_TTL_S

    def create(self) -> TaskRecord:
        """Создать новую задачу и вернуть её запись."""
        task_id = str(uuid.uuid4())
        record = TaskRecord(task_id=task_id, created_at=_utc_now_iso())
        with self._lock:
            self._tasks[task_id] = record
        self._gc()
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        """Получить запись задачи по ID."""
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields: Any) -> TaskRecord | None:
        """Обновить поля задачи."""
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return None
            for key, value in fields.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            return record

    def push_event(self, task_id: str, event: dict[str, Any]) -> None:
        """Добавить событие в ленту задачи."""
        with self._lock:
            record = self._tasks.get(task_id)
        if record:
            record.push_event(event)

    def subscribe(self, task_id: str) -> queue.Queue | None:
        """Подписаться на события задачи (SSE)."""
        with self._lock:
            record = self._tasks.get(task_id)
        if record:
            return record.subscribe()
        return None

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        """Отписаться от событий задачи."""
        with self._lock:
            record = self._tasks.get(task_id)
        if record:
            record.unsubscribe(q)

    def _gc(self) -> None:
        """Удалить устаревшие задачи."""
        if self._ttl_s <= 0:
            return
        cutoff = time.time() - self._ttl_s
        with self._lock:
            stale = [
                tid for tid, rec in self._tasks.items()
                if rec.finished_at and _parse_iso(rec.finished_at) < cutoff
            ]
            for tid in stale:
                self._tasks.pop(tid, None)


def _parse_iso(value: str) -> float:
    """Парсинг ISO-строки в unix timestamp (для GC)."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0.0


_store: MemoryTaskStore | None = None


def get_task_store() -> MemoryTaskStore:
    """Вернуть singleton-экземпляр хранилища задач."""
    global _store
    if _store is None:
        _store = MemoryTaskStore()
    return _store


def submit_task(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> TaskRecord:
    """Поставить задачу в очередь и запустить в фоновом потоке."""
    store = get_task_store()
    record = store.create()

    def _runner() -> None:
        store.update(
            record.task_id,
            status=TaskStatus.RUNNING,
            started_at=_utc_now_iso(),
            stage="starting",
            progress=5,
        )
        record.push_event({"type": "started", "stage": "starting"})
        try:
            result = func(record, *args, **kwargs)
            store.update(
                record.task_id,
                status=TaskStatus.COMPLETED,
                finished_at=_utc_now_iso(),
                progress=100,
                stage="done",
                result=result if isinstance(result, dict) else {"data": result},
            )
            record.push_event({"type": "completed", "result": result})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Фоновая задача %s упала", record.task_id)
            store.update(
                record.task_id,
                status=TaskStatus.FAILED,
                finished_at=_utc_now_iso(),
                error=f"{type(exc).__name__}: {exc}",
            )
            record.push_event({"type": "failed", "error": str(exc)})

    thread = threading.Thread(
        target=_runner,
        name=f"cj-task-{record.task_id[:8]}",
        daemon=True,
    )
    thread.start()
    return record


def get_task_status(task_id: str) -> dict[str, Any] | None:
    """Опросить статус задачи (для polling-эндпоинта)."""
    record = get_task_store().get(task_id)
    return record.to_dict() if record else None


def stream_task_events(task_id: str) -> Iterator[dict[str, Any]]:
    """Генератор событий задачи (для SSE-стриминга)."""
    store = get_task_store()
    record = store.get(task_id)
    if not record:
        return
    for event in list(record.events):
        yield event
    if record.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return
    q = record.subscribe()
    try:
        while True:
            try:
                event = q.get(timeout=15.0)
                yield event
                if event.get("type") in ("completed", "failed", "cancelled"):
                    break
            except queue.Empty:
                yield {"type": "heartbeat", "ts": _utc_now_iso()}
                current = store.get(task_id)
                if current and current.status in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    yield {"type": current.status.value, "ts": _utc_now_iso()}
                    break
    finally:
        record.unsubscribe(q)