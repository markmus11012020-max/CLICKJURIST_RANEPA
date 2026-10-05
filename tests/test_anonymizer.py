"""Регрессионные тесты для ``backend.core.anonymizer``.

Проверяем ключевые инварианты из раздела 4 ТЗ (152-ФЗ, STAGE_3):

    * плейсхолдеры ``[ФИО_1]`` и т.п. заменяются реальными ПДн из карты
      подстановок (``masking_metadata``);
    * документ, в котором НЕТ плейсхолдеров, возвращается без изменений
      (best-effort без падений);
    * пустая/повреждённая карта подстановок не вызывает исключений;
    * плейсхолдеры, для которых нет подстановки, остаются в тексте
      (явный сигнал оператору), а не приводят к падению;
    * карта в по-типовом формате ``{"ФИО": ["Иванов", "Петров"]}``
      автоматически приводится к плоскому ``{"ФИО_1": "Иванов", …}``.
"""

from __future__ import annotations

from backend.core.anonymizer import deanonymize_document


# ------------------------------------------------------------------------------
# Базовые сценарии
# ------------------------------------------------------------------------------
def test_deanonymize_replaces_simple_placeholder():
    """Один плейсхолдер ФИО заменяется на реальное значение."""
    md = "# Мировому судье судебного участка №1\n\nЗаявитель: [ФИО_1]\nТелефон: [ТЕЛЕФОН_1]\n"
    metadata = {
        "ФИО_1": "Иванов Иван Иванович",
        "ТЕЛЕФОН_1": "+7 (905) 123-45-67",
    }
    out = deanonymize_document(md, metadata)
    assert "Иванов Иван Иванович" in out
    assert "+7 (905) 123-45-67" in out
    assert "[ФИО_1]" not in out
    assert "[ТЕЛЕФОН_1]" not in out


def test_deanonymize_keeps_unknown_placeholders():
    """Плейсхолдеры, для которых нет подстановки, остаются в тексте."""
    md = "Заявитель: [ФИО_1], адрес: [АДРЕС_99]"
    metadata = {"ФИО_1": "Иванов Иван Иванович"}
    out = deanonymize_document(md, metadata)
    assert "Иванов Иван Иванович" in out
    # Без подстановки плейсхолдер остаётся — это сигнал оператору.
    assert "[АДРЕС_99]" in out


def test_deanonymize_handles_no_placeholders():
    """Текст без плейсхолдеров возвращается без изменений."""
    md = "# Исковое заявление\n\nПолный текст без ПДн."
    metadata = {"ФИО_1": "Иванов"}
    out = deanonymize_document(md, metadata)
    assert out == md


def test_deanonymize_with_empty_metadata():
    """Пустая карта — best-effort без падений."""
    md = "Текст [ФИО_1] и [АДРЕС_1]."
    assert deanonymize_document(md, {}) == md
    assert deanonymize_document(md, None) == md


def test_deanonymize_with_none_input_text():
    """Пустой/None текст не падает; возвращается пустая строка (best-effort)."""
    assert deanonymize_document("", {"ФИО_1": "Иванов"}) == ""
    assert deanonymize_document(None, {"ФИО_1": "Иванов"}) == ""


# ------------------------------------------------------------------------------
# Формат masking_metadata
# ------------------------------------------------------------------------------
def test_deanonymize_flattened_by_type_to_indexed():
    """По-типовой формат ``{ФИО: [str, ...]}`` нормализуется в плоский."""
    md = "Истец: [ФИО_1]\nОтветчик: [ФИО_2]"
    metadata = {"ФИО": ["Иванов Иван Иванович", "Петров Пётр Петрович"]}
    out = deanonymize_document(md, metadata)
    assert "Иванов Иван Иванович" in out
    assert "Петров Пётр Петрович" in out
    assert "[ФИО_1]" not in out
    assert "[ФИО_2]" not in out


def test_deanonymize_flattened_single_value():
    """Одиночное значение в по-типовом формате становится ``ФИО_1``."""
    md = "Заявитель: [ФИО_1]"
    metadata = {"ФИО": "Сидоров Сидор Сидорович"}
    out = deanonymize_document(md, metadata)
    assert "Сидоров Сидор Сидорович" in out
    assert "[ФИО_1]" not in out


def test_deanonymize_skips_empty_values():
    """Пустые строки в карте игнорируются, не затирают плейсхолдеры."""
    md = "Заявитель: [ФИО_1]"
    metadata = {"ФИО_1": "", "ФИО_2": "  "}
    out = deanonymize_document(md, metadata)
    # Пустая подстановка: оставляем плейсхолдер как есть.
    assert "[ФИО_1]" in out


# ------------------------------------------------------------------------------
# Устойчивость к граничным случаям
# ------------------------------------------------------------------------------
def test_deanonymize_does_not_crash_on_broken_metadata():
    """Битый формат значения (int / float / bool) приводится к строке."""
    md = "Сумма: [СУММА_1]"
    metadata = {"СУММА_1": 12345}
    out = deanonymize_document(md, metadata)
    assert "12345" in out


def test_deanonymize_realistic_full_document():
    """Реалистичный фрагмент искового заявления."""
    md = (
        "В Мировой суд судебного участка № 142\n\n"
        "Истец: [ФИО_1], проживающий по адресу [АДРЕС_1],\n"
        "тел.: [ТЕЛЕФОН_1], e-mail: [EMAIL_1].\n\n"
        "Ответчик: [ФИО_2], зарегистрирован по адресу [АДРЕС_2].\n"
    )
    metadata = {
        "ФИО_1": "Иванов Иван Иванович",
        "ФИО_2": "Петров Пётр Петрович",
        "АДРЕС_1": "г. Самара, ул. Ленина, д. 1, кв. 23",
        "АДРЕС_2": "г. Самара, ул. Победы, д. 5, кв. 11",
        "ТЕЛЕФОН_1": "+7 (905) 123-45-67",
        "EMAIL_1": "client@example.ru",
    }
    out = deanonymize_document(md, metadata)
    # Все подстановки применены.
    for value in metadata.values():
        assert value in out
    # Никаких плейсхолдеров не осталось.
    assert "[ФИО_1]" not in out
    assert "[ФИО_2]" not in out
    assert "[АДРЕС_1]" not in out
    assert "[АДРЕС_2]" not in out
    assert "[ТЕЛЕФОН_1]" not in out
    assert "[EMAIL_1]" not in out
