"""Zero-Storage Logging (раздел 2 ТЗ).

Требования:
    * журналы маршрутизируются через **Yandex Cloud Logging**;
    * сырые персональные данные СТРОГО запрещены в логах;
    * разрешено записывать только анонимизированный UUID сессии.

Реализация состоит из трёх слоёв защиты:
    1. :class:`PIIRedactingFilter` — вычищает ПДн из любого сообщения
       (страховка: даже если разработчик случайно передал текст запроса в лог).
    2. :class:`JsonFormatter` — структурированный JSON-вывод для Cloud Logging.
    3. :class:`YandexCloudLoggingHandler` — асинхронная (фоновая) отправка
       записей в API Yandex Cloud Logging батчами.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import threading
import time

import requests

# --- 1. Маскировка ПДн в логах ------------------------------------------------
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Паспорт РФ: 45 06 123456 / 4506 123456
    (re.compile(r"\b\d{2}\s?\d{2}\s?\d{6}\b"), "[PASSPORT]"),
    # Телефон: +7 (999) 123-45-67, 8-999-123-45-67, 79991234567
    (re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"), "[PHONE]"),
    # E-mail
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    # СНИЛС: 123-456-789 00
    (re.compile(r"\b\d{3}-\d{3}-\d{3}\s?\d{2}\b"), "[SNILS]"),
    # ИНН (10 или 12 цифр)
    (re.compile(r"\b\d{10}\b|\b\d{12}\b"), "[INN]"),
    # Длинные числовые идентификаторы
    (re.compile(r"\b\d{6,}\b"), "[NUMBER]"),
)


def redact_text(text: str) -> str:
    """Заменить персональные данные в произвольном тексте на плейсхолдеры."""
    if not text:
        return text
    result = text
    for pattern, replacement in _PII_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class PIIRedactingFilter(logging.Filter):
    """Фильтр, который вычищает ПДн из сообщений журнала."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: redact_text(str(value))
                        for key, value in record.args.items()
                    }
                else:
                    record.args = tuple(redact_text(str(arg)) for arg in record.args)
        except Exception:  # pragma: no cover — журнал не должен ломать приложение
            return True
        return True


# --- 2. Форматтер -------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """Формат JSON-строки для Yandex Cloud Logging."""

    def __init__(self, service: str = "clickjurist") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Единственный идентификатор пользователя, допустимый в логах, — UUID сессии
        session_id = getattr(record, "session_id", None)
        if session_id:
            payload["session_id"] = session_id
        for key in ("endpoint", "status_code", "latency_ms", "provider", "stage"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# --- 3. Отправка в Yandex Cloud Logging --------------------------------------
class YandexCloudLoggingHandler(logging.Handler):
    """Фоновый обработчик, отправляющий логи в Yandex Cloud Logging."""

    FLUSH_INTERVAL_S = 5
    MAX_BATCH = 100

    def __init__(self, log_group_id: str, url: str) -> None:
        super().__init__()
        self.log_group_id = log_group_id
        self.url = url
        self._queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, name="yc-log-shipper", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        """Положить запись в очередь на отправку (никогда не блокирует)."""
        try:
            self._queue.put_nowait(
                {
                    "json_payload": json.loads(self.format(record)),
                    "timestamp": time.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(record.created)
                    ),
                }
            )
        except Exception:  # pragma: no cover — логи не должны ломать сервис
            pass

    def _worker(self) -> None:
        buffer: list[dict[str, object]] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                buffer.append(self._queue.get(timeout=self.FLUSH_INTERVAL_S))
            except queue.Empty:
                pass
            if buffer and (
                len(buffer) >= self.MAX_BATCH
                or (time.time() - last_flush) >= self.FLUSH_INTERVAL_S
            ):
                self._ship(buffer)
                buffer = []
                last_flush = time.time()

    def _ship(self, entries: list[dict[str, object]]) -> None:
        """Отправить батч записей через Logging API (best-effort)."""
        token = _iam_token()
        if not token:
            return
        body = {
            "entries": [
                {
                    **entry,
                    "resource": {
                        "type": "serverless_container",
                        "id": self.log_group_id,
                    },
                }
                for entry in entries
            ]
        }
        try:
            requests.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=5,
            )
        except requests.RequestException:
            pass  # логирование не должно влиять на работоспособность сервиса

    def close(self) -> None:  # noqa: D102
        self._stop.set()
        super().close()


def _iam_token() -> str:
    """IAM-токен для Logging API (переиспользуем логику KMS-модуля)."""
    try:
        from backend.yandex_kms import get_iam_token

        return get_iam_token()
    except Exception:  # pragma: no cover
        return os.getenv("YANDEX_IAM_TOKEN", "").strip()


# --- Публичный API ------------------------------------------------------------
def setup_logging() -> logging.Logger:
    """Сконфигурировать корневой логгер проекта и вернуть его."""
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = JsonFormatter()
    redactor = PIIRedactingFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    root.addHandler(stream)

    if os.getenv("YANDEX_LOGGING_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        group_id = os.getenv("YANDEX_LOG_GROUP_ID", "").strip()
        if group_id:
            yc_handler = YandexCloudLoggingHandler(
                log_group_id=group_id,
                url=os.getenv(
                    "YANDEX_LOGGING_URL",
                    "https://logging.api.cloud.yandex.net/logging/v1/entries:write",
                ),
            )
            yc_handler.setFormatter(formatter)
            yc_handler.addFilter(redactor)
            root.addHandler(yc_handler)

    logger = logging.getLogger("clickjurist")
    logger.info("Система логирования инициализирована (Zero-Storage режим)")
    return logger