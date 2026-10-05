"""Anti-abuse: sliding-window rate-limit для дорогих эндпоинтов.

Дополняет платёжный барьер (``backend.api.deps.session_gate``): даже платный
пользователь не должен иметь возможность слать 1000 запросов в минуту.
Два независимых лимита работают параллельно — оба должны пропустить запрос.

Архитектура:
- Один процесс = один in-memory словарь. При multi-worker deployment (uvicorn
  --workers N) каждый воркер держит свой счётчик, поэтому суммарный лимит
  умножается на N. Для одного воркера (текущий деплой) это роли не играет.
- ``time.monotonic()`` для устойчивости к переводу системных часов.
- Дека по ключу — хранит временные метки запросов в окне. Истёкшие
  подчищаются лениво при следующем ``check()`` и явно в ``cleanup()``.

Что НЕ покрывает:
- Распределённую атаку с разных IP (для этого нужен Redis / shared store).
- Капчу/turnstile — это уровень выше, для production включается отдельно.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _LimitConfig:
    """Параметры одного лимита. Передаётся в ``SlidingWindowCounter``."""

    name: str
    max_requests: int
    window_seconds: int


class SlidingWindowCounter:
    """Счётчик скользящего окна.

    Потокобезопасен (``threading.Lock``). Ключ — произвольная строка
    (``"session:abc..."``, ``"ip:1.2.3.4"``).
    """

    def __init__(self, config: _LimitConfig) -> None:
        self._cfg = config
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._cfg.name

    def check(self, key: str) -> tuple[bool, int]:
        """Зарегистрировать запрос и решить, проходит ли он.

        Returns:
            ``(allowed, retry_after_seconds)``. Если ``allowed=False`` —
            запрос нужно прервать ответом 429 и заголовком ``Retry-After``.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            # Подчищаем всё, что вышло за окно — ленивая сборка мусора.
            cutoff = now - self._cfg.window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._cfg.max_requests:
                # Сколько секунд осталось до выхода из окна самого старого
                # запроса. Округляем вверх и прибавляем 1с запас.
                retry = int(bucket[0] + self._cfg.window_seconds - now) + 1
                return False, max(1, retry)
            bucket.append(now)
            return True, 0

    def cleanup(self) -> int:
        """Удалить пустые и полностью истёкшие ключи. Возвращает число
        удалённых ключей — удобно для периодического логирования/мониторинга."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            for key, bucket in list(self._buckets.items()):
                cutoff = now - self._cfg.window_seconds
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if not bucket:
                    del self._buckets[key]
                    removed += 1
        return removed

    def snapshot(self) -> dict[str, int]:
        """Диагностика: число «активных» ключей и запросов в каждом. Только
        для логов/метрик, не использовать в горячем пути."""
        with self._lock:
            return {k: len(v) for k, v in self._buckets.items()}


# ---------------------------------------------------------------------------
# Глобальные экземпляры — инициализируются при старте через init_limiters().
# Хранятся как None до инициализации, чтобы не схлопотать ImportError, если
# конфиг ещё не загружен.
# ---------------------------------------------------------------------------
session_limiter: SlidingWindowCounter | None = None
ip_limiter: SlidingWindowCounter | None = None


def init_limiters(
    session_per_hour: int,
    ip_per_min: int,
) -> None:
    """Поднять singleton-счётчики. Вызывать один раз при старте приложения.

    Если значение 0 — соответствующий лимит отключается (счётчик создаётся
    с ``max_requests=2**31`` — фактически «не ограничивать»). Полезно для
    локальной отладки, чтобы не спотыкаться о лимиты.
    """
    global session_limiter, ip_limiter
    sentinel = 2**31
    session_limiter = SlidingWindowCounter(
        _LimitConfig(
            name="session",
            max_requests=session_per_hour if session_per_hour > 0 else sentinel,
            window_seconds=3600,
        )
    )
    ip_limiter = SlidingWindowCounter(
        _LimitConfig(
            name="ip",
            max_requests=ip_per_min if ip_per_min > 0 else sentinel,
            window_seconds=60,
        )
    )
    logger.info(
        "Rate-limit init: session=%d/час, ip=%d/мин",
        session_per_hour,
        ip_per_min,
    )


def get_client_ip(request) -> str:
    """Достать IP клиента с учётом reverse-proxy.

    Приоритет:
    1. ``X-Forwarded-For`` (первый из списка) — стандарт для прокси/CDN.
    2. ``X-Real-IP`` — fallback для nginx.
    3. ``request.client.host`` — прямое соединение.
    Возвращает ``"unknown"`` если ничего нет (тесты / unix-socket).
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Берём первый — это IP клиента, дальше идут прокси.
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"