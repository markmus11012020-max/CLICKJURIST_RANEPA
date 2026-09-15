"""Регрессионные тесты платёжного барьера и автоматической регистрации сессий.

Проверяем ключевые инварианты из ТЗ:
    * при первом обращении по новому ``session_hash`` запись в БД создаётся
      автоматически со статусом ``is_free = 1`` (бесплатный запрос доступен);
    * первый запрос проходит без 402;
    * запрос с истёкшим/отсутствующим оплаченным доступом после исчерпания
      бесплатного лимита возвращает 402 с корректным телом ``PaymentRequired``.

Тесты выполнены на уровне ``store``/эндпоинтов напрямую — без ``TestClient``,
поскольку кастомный middleware пишет в логгер, что конфликтует с pytest-овским
log-capture handler-ом (см. ошибку httpx ``logger.info`` formatting).
"""
from __future__ import annotations

from backend.db import store
from backend.main import _session_gate, _payment_required


def _session_hash_for(fp: str = "test-fp-new-session") -> str:
    """Посчитать тот же ``session_hash``, что и продакшн-код."""
    from backend.security import compute_session_hash

    return compute_session_hash("203.0.113.7", fp)


class _FakeRequest:
    """Минимальный объект запроса, который принимает ``session_context``."""

    def __init__(self, fingerprint: str) -> None:
        self.headers = {"x-forwarded-for": "203.0.113.7", "x-client-fingerprint": fingerprint}
        self.client = type("C", (), {"host": "203.0.113.7"})()


def test_new_session_is_created_with_free_tier():
    """Запись о новой сессии автоматически создаётся в БД (is_free=1)."""
    session_hash = _session_hash_for("test-fp-new-session-1")

    # До вызова ensure_session в БД нет записи.
    assert store.get_session(session_hash) is None

    # Эмулируем работу /api/session: первый запрос автоматически создаёт запись.
    session = store.ensure_session(session_hash)
    assert int(session["is_free"]) == 1
    assert int(session["free_requests_used"]) == 0

    persisted = store.get_session(session_hash)
    assert persisted is not None
    assert int(persisted["is_free"]) == 1


def test_new_session_can_use_one_free_request():
    """Только что созданная сессия имеет право на один бесплатный запрос."""
    session_hash = _session_hash_for("test-fp-new-session-2")
    assert store.can_use_free_request(session_hash) is True


def test_session_gate_grants_free_access_to_new_visitor():
    """``_session_gate`` для новой сессии НЕ возвращает 402.

    Возвращает ``(hash, session_id, was_free=True, denial=None)`` — это сигнал
    эндпоинту пропустить запрос и выдать 200 OK.
    """
    request = _FakeRequest("test-fp-new-session-3")
    session_hash, session_id, was_free, denial = _session_gate(request, "consultation")

    assert denial is None, "Новая сессия не должна блокироваться paywall'ом"
    assert was_free is True
    assert len(session_id) == 36
    # Запись о сессии фактически создана в БД.
    persisted = store.get_session(session_hash)
    assert persisted is not None
    assert int(persisted["is_free"]) == 1


def test_session_gate_returns_402_after_free_tier_consumed():
    """После списания бесплатного запроса ``_session_gate`` возвращает 402."""
    request = _FakeRequest("test-fp-new-session-4")

    # 1) Создаём сессию и списываем бесплатный лимит (как это делает /api/query).
    session_hash, _, _, _ = _session_gate(request, "consultation")
    store.consume_free_request(session_hash)

    # 2) Повторный прогон гейта теперь должен вернуть denial с кодом 402.
    _, _, was_free, denial = _session_gate(request, "consultation")
    assert was_free is False
    assert denial is not None
    assert denial.status_code == 402
    # Robokassa-структура ответа 402 соответствует PaymentRequiredResponse.
    import json
    body = json.loads(denial.body.decode("utf-8"))
    assert body["service"] == "consultation"
    assert body["payment_url"].startswith("https://")
    assert "inv_id" in body and body["inv_id"]


def test_session_gate_still_grants_after_paid_until_set():
    """Оплаченный доступ пропускает запрос даже без бесплатного лимита."""
    request = _FakeRequest("test-fp-new-session-5")

    # Создаём сессию, списываем бесплатный лимит, выдаём платный доступ.
    session_hash, _, _, _ = _session_gate(request, "consultation")
    store.consume_free_request(session_hash)
    store.grant_paid_access(session_hash, days=30)

    _, _, was_free, denial = _session_gate(request, "consultation")
    assert denial is None
    assert was_free is False


def test_payment_required_helper_builds_valid_payload():
    """``_payment_required`` собирает корректное тело 402 ответа."""
    session_hash = _session_hash_for("test-fp-new-session-6")
    resp = _payment_required("checklist", session_hash)
    assert resp.status_code == 402
    import json
    body = json.loads(resp.body.decode("utf-8"))
    assert body["service"] == "checklist"
    assert body["payment_url"].startswith("https://")
    assert body["amount"] >= 1
from backend.config import robokassa_settings
from backend.services.robokassa import (
    payment_signature,
    success_signature,
    result_signature,
    verify_result_signature,
    verify_success_signature,
)


def test_settings_have_test_flag():
    cfg = robokassa_settings()
    assert cfg.test_param in (0, 1)
    assert cfg.payment_url.startswith("https://")


def test_absolute_url_from_path():
    cfg = robokassa_settings()
    url = cfg.absolute("/api/payment/success")
    assert url.startswith("http")
    assert "/api/payment/success" in url


def test_payment_signature_format():
    cfg = robokassa_settings()
    sig = payment_signature(cfg, "100.00", "12345")
    assert len(sig) == 32


def test_success_signature_roundtrip():
    cfg = robokassa_settings()
    out_sum = "100.00"
    inv_id = "12345"
    sig = success_signature(cfg, out_sum, inv_id)
    assert verify_success_signature(out_sum, inv_id, sig)


def test_result_signature_roundtrip():
    cfg = robokassa_settings()
    out_sum = "100.00"
    inv_id = "12345"
    sig = result_signature(cfg, out_sum, inv_id)
    assert verify_result_signature(out_sum, inv_id, sig)
