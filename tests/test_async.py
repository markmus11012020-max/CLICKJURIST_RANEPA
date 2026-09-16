"""Тесты асинхронной генерации, JWT-авторизации и guardrails (разделы 2.1, 3.1, 5.2 ТЗ)."""
from __future__ import annotations

import time

from backend import jwt_auth, task_store
from backend.services import guardrails


# ------------------------------------------------------------------------------
# JWT-авторизация (раздел 3.1 ТЗ)
# ------------------------------------------------------------------------------
def test_jwt_create_and_decode_roundtrip():
    """Созданный токен декодируется без ошибок."""
    token, session_uuid, expires = jwt_auth.create_session_token(
        fingerprint="test-fp-12345",
    )
    assert token
    assert session_uuid
    payload = jwt_auth.decode_session_token(token)
    assert payload is not None
    assert payload["sub"] == session_uuid
    assert "exp" in payload
    assert "iat" in payload


def test_jwt_invalid_token_returns_none():
    """Невалидный токен возвращает None, а не исключение."""
    assert jwt_auth.decode_session_token("") is None
    assert jwt_auth.decode_session_token("invalid.token.here") is None


def test_jwt_fingerprint_hash_is_stable():
    """Хеш отпечатка стабилен для одного и того же ввода."""
    h1 = jwt_auth.fingerprint_hash("fp-abc")
    h2 = jwt_auth.fingerprint_hash("fp-abc")
    assert h1 == h2
    assert len(h1) == 32


def test_jwt_fingerprint_hash_differs_for_different_inputs():
    """Разные отпечатки дают разные хеши."""
    h1 = jwt_auth.fingerprint_hash("fp-abc")
    h2 = jwt_auth.fingerprint_hash("fp-xyz")
    assert h1 != h2


# ------------------------------------------------------------------------------
# Фоновые задачи (раздел 2.1 ТЗ)
# ------------------------------------------------------------------------------
def test_task_store_create_and_get():
    """Создание и получение задачи."""
    store = task_store.get_task_store()
    record = store.create()
    assert record.task_id
    assert record.status == task_store.TaskStatus.PENDING

    fetched = store.get(record.task_id)
    assert fetched is not None
    assert fetched.task_id == record.task_id


def test_task_store_update_fields():
    """Обновление полей задачи."""
    store = task_store.get_task_store()
    record = store.create()
    store.update(
        record.task_id,
        status=task_store.TaskStatus.RUNNING,
        progress=50,
        stage="masking",
    )
    fetched = store.get(record.task_id)
    assert fetched.status == task_store.TaskStatus.RUNNING
    assert fetched.progress == 50
    assert fetched.stage == "masking"


def test_task_store_push_event():
    """Добавление событий в ленту задачи."""
    store = task_store.get_task_store()
    record = store.create()
    store.push_event(record.task_id, {"type": "progress", "stage": "test"})
    fetched = store.get(record.task_id)
    assert len(fetched.events) == 1
    assert fetched.events[0]["type"] == "progress"


def test_submit_task_runs_in_background():
    """submit_task запускает функцию в фоне и обновляет статус."""
    def my_task(record, value):
        store = task_store.get_task_store()
        store.update(record.task_id, progress=50)
        return {"value": value * 2}

    record = task_store.submit_task(my_task, 21)
    for _ in range(50):
        time.sleep(0.1)
        fetched = task_store.get_task_store().get(record.task_id)
        if fetched.status in (
            task_store.TaskStatus.COMPLETED,
            task_store.TaskStatus.FAILED,
        ):
            break
    assert fetched.status == task_store.TaskStatus.COMPLETED
    assert fetched.result == {"value": 42}


def test_submit_task_captures_exception():
    """submit_task ловит исключения и помечает задачу как failed."""
    def failing_task(record):
        raise RuntimeError("test error")

    record = task_store.submit_task(failing_task)
    for _ in range(50):
        time.sleep(0.1)
        fetched = task_store.get_task_store().get(record.task_id)
        if fetched.status in (
            task_store.TaskStatus.COMPLETED,
            task_store.TaskStatus.FAILED,
        ):
            break
    assert fetched.status == task_store.TaskStatus.FAILED
    assert "test error" in fetched.error


def test_get_task_status_returns_dict():
    """get_task_status возвращает публичный dict."""
    record = task_store.submit_task(lambda r: {"ok": True})
    status = task_store.get_task_status(record.task_id)
    assert status is not None
    assert status["task_id"] == record.task_id
    assert "status" in status
    assert "progress" in status


def test_get_task_status_unknown_returns_none():
    """Несуществующий task_id возвращает None."""
    assert task_store.get_task_status("non-existent-id") is None


# ------------------------------------------------------------------------------
# Guardrails (раздел 5.2 ТЗ)
# ------------------------------------------------------------------------------
def test_guardrails_valid_text_passes():
    """Корректный текст проходит проверку."""
    text = (
        "Согласно ст. 15 ГК РФ лицо обязано возместить убытки. "
        "В соответствии со ст. 196 ГК РФ общий срок исковой давности 3 года."
    )
    report = guardrails.validate_consultation(text)
    assert report.passed is True
    assert len(report.violations) == 0


def test_guardrails_detects_invalid_article():
    """Несуществующий номер статьи — нарушение."""
    text = "Согласно ст. 9999 ГК РФ лицо обязано..."
    report = guardrails.validate_consultation(text)
    assert report.passed is False
    assert any(v.kind == "invalid_article" for v in report.violations)


def test_guardrails_detects_forbidden_pattern():
    """Выдуманные суммы штрафов — нарушение."""
    text = "Штраф в размере 50000 рублей по ст. 15 ГК РФ."
    report = guardrails.validate_consultation(text)
    assert report.passed is False
    assert any(v.kind == "forbidden_pattern" for v in report.violations)


def test_guardrails_detects_missing_placeholder():
    """Отсутствие плейсхолдера из исходного запроса — нарушение."""
    text = "Согласно ст. 15 ГК РФ лицо обязано возместить убытки."
    masked = "Иван Иванов позвонил [NAME_1] и сказал..."
    report = guardrails.validate_consultation(text, masked_query=masked)
    assert report.passed is False
    assert any(v.kind == "missing_placeholder" for v in report.violations)


def test_guardrails_checklist_requires_4_steps():
    """Чек-лист без 4 шагов — нарушение."""
    text = "Шаг 1. Соберите документы."
    report = guardrails.validate_checklist(text)
    assert report.passed is False
    assert any(v.kind == "missing_header" for v in report.violations)


def test_guardrails_should_regenerate_critical():
    """Критические нарушения требуют регенерации."""
    text = "Согласно ст. 9999 ГК РФ..."
    report = guardrails.validate_consultation(text)
    assert guardrails.should_regenerate(report) is True


def test_guardrails_should_not_regenerate_minor():
    """Некритические нарушения не требуют регенерации."""
    text = "Согласно ст. 15 ГК РФ лицо обязано возместить убытки."
    report = guardrails.validate_checklist(text)
    assert report.passed is False
    assert guardrails.should_regenerate(report) is False


def test_guardrails_empty_text():
    """Пустой текст — нарушение."""
    report = guardrails.validate_consultation("")
    assert report.passed is False
    assert any(v.kind == "empty" for v in report.violations)


# ------------------------------------------------------------------------------
# Веб-фактчекинг: приоритетные домены (раздел 2.2 ТЗ)
# ------------------------------------------------------------------------------
def test_priority_domain_detection():
    """Распознавание приоритетных доменов."""
    from backend.services import web_factcheck

    assert web_factcheck.is_priority_domain("https://www.consultant.ru/doc/123") is True
    assert web_factcheck.is_priority_domain("https://garant.ru/article/456") is True
    assert web_factcheck.is_priority_domain("https://pravo.gov.ru/abc") is True
    assert web_factcheck.is_priority_domain("https://example.com/article") is False
    assert web_factcheck.is_priority_domain("") is False


def test_clean_html_strips_tags():
    """Очистка HTML от тегов."""
    from backend.services import web_factcheck

    html = "<html><body><h1>Заголовок</h1><p>Текст <b>жирный</b>.</p></body></html>"
    text = web_factcheck.clean_html(html)
    assert "<" not in text
    assert "Заголовок" in text
    assert "Текст" in text
    assert "жирный" in text


def test_clean_html_removes_scripts():
    """Скрипты и стили удаляются из HTML."""
    from backend.services import web_factcheck

    html = "<script>alert('xss')</script><p>Видимый текст</p>"
    text = web_factcheck.clean_html(html)
    assert "alert" not in text
    assert "Видимый текст" in text


def test_generate_search_queries():
    """Генерация поисковых запросов из резюме."""
    from backend.services import web_factcheck

    queries = web_factcheck.generate_search_queries(
        "Спор о задержке зарплаты работодателем"
    )
    assert len(queries) >= 2
    assert all("закон РФ" in q or "практика" in q or "ГК РФ" in q for q in queries)


def test_prioritize_sources_orders_priority_first():
    """Приоритетные домены идут первыми."""
    from backend.services import web_factcheck

    sources = [
        web_factcheck.WebSource(title="A", url="https://example.com/a"),
        web_factcheck.WebSource(title="B", url="https://consultant.ru/b"),
        web_factcheck.WebSource(title="C", url="https://other.com/c"),
    ]
    prioritized = web_factcheck.prioritize_sources(sources)
    assert prioritized[0].url == "https://consultant.ru/b"


# ------------------------------------------------------------------------------
# Динамические оговорки (раздел 5.1 ТЗ)
# ------------------------------------------------------------------------------
def test_dynamic_disclaimer_added():
    """Оговорка добавляется к тексту."""
    from backend.services.prompts import with_dynamic_disclaimer

    result = with_dynamic_disclaimer("Срок исковой давности 1 год.")
    assert "Юридическая оговорка" in result
    assert "ст. 181 ГК РФ" in result


def test_dynamic_disclaimer_not_duplicated():
    """Оговорка не дублируется при повторном вызове."""
    from backend.services.prompts import with_dynamic_disclaimer

    text = "Срок 1 год."
    once = with_dynamic_disclaimer(text)
    twice = with_dynamic_disclaimer(once)
    assert once == twice


def test_dynamic_disclaimer_empty_text():
    """Пустой текст возвращается без изменений."""
    from backend.services.prompts import with_dynamic_disclaimer

    assert with_dynamic_disclaimer("") == ""