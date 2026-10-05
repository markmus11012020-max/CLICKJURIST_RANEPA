"""Тесты фильтра маскирования ПДн в логах.

Регрессия: фильтр раньше приводил ВСЕ аргументы записи к строке, из-за
чего шаблоны вида ``"%d мс"`` падали с ``TypeError: %d format: a real
number is required, not str``. Ошибка возникала в самом логировании,
маскировала настоящую причину сбоя и теряла запись.
"""

from __future__ import annotations

import logging

import pytest

from backend.logging_setup import PIIRedactingFilter, redact_text, setup_logging


def _make_record(msg: str, args: tuple | dict) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# Маскирование
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("телефон +7 (999) 123-45-67", "[PHONE]"),
        ("ИНН 7707083893", "[INN]"),
        ("почта user@example.com", "[EMAIL]"),
        ("паспорт 45 06 123456", "[PASSPORT]"),
    ],
)
def test_redact_masks_pii(raw: str, expected: str) -> None:
    assert expected in redact_text(raw)


def test_ten_digit_number_is_not_labelled_as_passport() -> None:
    """Регрессия: 10 цифр без разделителей — это ИНН, а не паспорт."""
    out = redact_text("ИНН 7707083893")
    assert "[PASSPORT]" not in out
    assert "[INN]" in out


def test_filter_masks_pii_in_message_and_args() -> None:
    record = _make_record(
        "Позвоните +7 (999) 123-45-67 по номеру %s",
        ("договор 7707083893",),
    )
    PIIRedactingFilter().filter(record)
    message = record.getMessage()
    assert "[PHONE]" in message
    assert "[INN]" in message


# ---------------------------------------------------------------------------
# Регрессия: типы аргументов должны сохраняться
# ---------------------------------------------------------------------------
def test_filter_keeps_integer_args_for_percent_d() -> None:
    """``%d`` требует число: фильтр обязан оставить int как int."""
    record = _make_record("Готово за %d мс, %d байт", (92, 44567))
    PIIRedactingFilter().filter(record)
    assert record.getMessage() == "Готово за 92 мс, 44567 байт"


def test_filter_keeps_float_args_for_percent_f() -> None:
    record = _make_record("Доля ошибок: %.2f", (0.125,))
    PIIRedactingFilter().filter(record)
    assert record.getMessage() == "Доля ошибок: 0.12"


def test_filter_keeps_bool_and_none() -> None:
    record = _make_record("flag=%s value=%s", (True, None))
    PIIRedactingFilter().filter(record)
    assert record.getMessage() == "flag=True value=None"


def test_filter_keeps_mapping_args_typed() -> None:
    # logging принимает mapping-аргументы только внутри кортежа из одного
    # элемента — иначе он трактует dict как позиционные аргументы.
    record = _make_record("latency=%(ms)d", ({"ms": 120},))
    PIIRedactingFilter().filter(record)
    assert record.getMessage() == "latency=120"


def test_filter_still_masks_strings_inside_tuple() -> None:
    record = _make_record("user=%s", ("+7 999 123-45-67",))
    PIIRedactingFilter().filter(record)
    assert "[PHONE]" in record.getMessage()
    assert "999 123-45-67" not in record.getMessage()


# ---------------------------------------------------------------------------
# Интеграция с реальной сборкой логов
# ---------------------------------------------------------------------------
def test_percent_d_works_through_setup_logging() -> None:
    """Сквозная проверка: запись с ``%d`` не теряется после setup_logging()."""
    setup_logging()
    logger = logging.getLogger("кликюрист.regression")
    try:
        # До исправления этот вызов поднимал TypeError внутри emit().
        logger.info("Сборка завершена: %d мс", 92)
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
