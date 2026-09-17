"""Тесты мегапайплайна в оффлайн-режиме (MASKING_PROVIDER=regex)."""
from backend.services.pii_masker import mask_query
from backend.services.llm_chain import attach_disclaimer, run_pipeline


def test_masking_removes_phone():
    mask = mask_query("Позвоните мне +7 (999) 123-45-67 пожалуйста")
    assert "999" not in mask.masked_query
    assert "[PHONE" in mask.masked_query or "+7" not in mask.masked_query


def test_masking_removes_email():
    mask = mask_query("Напишите на ivan.petrov@example.com для связи")
    assert "ivan.petrov" not in mask.masked_query
    assert "@example" not in mask.masked_query


def test_masking_keeps_legal_context():
    """Дата и правовая норма сохраняются после маскировки."""
    mask = mask_query("По ст. 15 ГК РФ с меня требуют 50000 рублей с 01.01.2024")
    assert "ГК РФ" in mask.masked_query


def test_attach_disclaimer_appends_text():
    text = "Это юридический ответ."
    out = attach_disclaimer(text)
    assert "Генеративный ИИ" in out
    assert text in out


def test_attach_disclaimer_skipped_for_documents():
    """Для официальных документов дисклеймер НЕ добавляется."""
    text = "Это текст искового заявления."
    out = attach_disclaimer(text, for_document=True)
    assert "Генеративный ИИ" not in out
    assert "ClickJurist" not in out
    assert out == text


def test_run_pipeline_offline_returns_error_without_providers():
    """Без API-ключей пайплайн возвращает структурированную ошибку."""
    result = run_pipeline("Соседи шумят ночью", with_stage2=True)
    assert result.error is not None
    assert "не сконфигурирован" in result.error or "контур" in result.error.lower()
    assert result.anonymized is True