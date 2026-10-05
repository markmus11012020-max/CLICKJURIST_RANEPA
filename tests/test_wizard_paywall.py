"""Регрессионные тесты paywall-логики на STAGE_3 (раздел 4 ТЗ, 152-ФЗ).

Проверяем ключевые инварианты:

    * ``GET /api/wizard/state`` для неоплаченной сессии на STAGE_3
      возвращает только безопасное превью документа (первые 30%
      + маркер ``<<<PAYWALL_DOCUMENT>>>``); реальная подписная часть
      НЕ утекает в ответ;
    * после ``is_paid=True`` тот же эндпоинт возвращает размаскированный
      Markdown (с реальными ПДн вместо плейсхолдеров);
    * при первом успешном deanonymize-вызове сессия переносится в
      холодную таблицу ``wizard_cases``;
    * сессии без документа (STAGE_1/STAGE_2) paywall-логика не затрагивает.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from backend import wizard_store
from backend.jwt_auth import create_session_token
from backend.models import WizardSession


@pytest.fixture()
def client() -> TestClient:
    """Полноценное приложение для проверки эндпоинта /api/wizard/state."""
    from backend.main import app

    return TestClient(app)


def _auth_cookie(session_uuid: str) -> dict[str, str]:
    """Сгенерировать cookie с JWT-токеном для указанной Wizard-сессии."""
    token, _, _ = create_session_token(session_uuid=session_uuid)
    return {"cj_session": token}


# ------------------------------------------------------------------------------
# Базовый paywall-флоу
# ------------------------------------------------------------------------------
def test_wizard_state_returns_paywall_preview_for_unpaid_session(client: TestClient):
    """STAGE_3 без оплаты: документ обрезается и помечается маркером paywall."""
    session_uuid = str(uuid.uuid4())
    full_md = (
        "# Заголовок документа\n\n"
        "Это первая часть — шапка с описанием.\n\n"
        + ("Длинная подписная часть с деталями. " * 200)
    )
    wizard_store.save_session(
        WizardSession(
            session_id=uuid.UUID(session_uuid),
            current_stage="STAGE_3",
            final_document_markdown=full_md,
            masking_metadata={"ФИО_1": "Иванов Иван Иванович"},
            is_paid=False,
        )
    )

    response = client.get(
        "/api/wizard/state",
        cookies=_auth_cookie(session_uuid),
        headers={"x-client-fingerprint": "test-fp-1"},
    )
    assert response.status_code == 200
    data = response.json()

    md = data["session"]["final_document_markdown"]
    assert md is not None
    # Шапка должна быть в превью.
    assert "# Заголовок документа" in md
    # Маркер paywall обязателен.
    assert "<<<PAYWALL_DOCUMENT>>>" in md
    # Превью должно быть КОРОЧЕ оригинала — большая часть «просительной»
    # части НЕ утекает наружу.
    assert len(md) < len(full_md)
    # is_paid в ответе False.
    assert data["session"]["is_paid"] is False
    # Превью не должно содержать весь исходный документ.
    assert md.strip() != full_md.strip()


def test_wizard_state_returns_deanonymized_text_for_paid_session(client: TestClient):
    """STAGE_3 с оплатой: документ deanonymized, плейсхолдеры заменены."""
    session_uuid = str(uuid.uuid4())
    full_md = (
        "# Заголовок\n\n"
        "Заявитель: [ФИО_1]\nАдрес: [АДРЕС_1]\n"
    )
    wizard_store.save_session(
        WizardSession(
            session_id=uuid.UUID(session_uuid),
            current_stage="STAGE_3",
            final_document_markdown=full_md,
            masking_metadata={
                "ФИО_1": "Иванов Иван Иванович",
                "АДРЕС_1": "г. Самара, ул. Ленина, д. 1",
            },
            is_paid=True,
            payment_inv_id="12345",
        )
    )

    response = client.get(
        "/api/wizard/state",
        cookies=_auth_cookie(session_uuid),
        headers={"x-client-fingerprint": "test-fp-2"},
    )
    assert response.status_code == 200
    data = response.json()

    md = data["session"]["final_document_markdown"]
    assert md is not None
    # Реальные ПДн подставлены.
    assert "Иванов Иван Иванович" in md
    assert "г. Самара, ул. Ленина, д. 1" in md
    # Плейсхолдеров в тексте быть не должно.
    assert "[ФИО_1]" not in md
    assert "[АДРЕС_1]" not in md
    # Маркер paywall не должен появиться — документ полностью deanonymized.
    assert "<<<PAYWALL_DOCUMENT>>>" not in md
    assert data["session"]["is_paid"] is True


def test_wizard_state_persists_to_cold_after_paid_deanonymization(client: TestClient):
    """После успешной deanonymization сессия переезжает в ``wizard_cases``."""
    session_uuid = str(uuid.uuid4())
    wizard_store.save_session(
        WizardSession(
            session_id=uuid.UUID(session_uuid),
            current_stage="STAGE_3",
            final_document_markdown="Документ [ФИО_1]",
            masking_metadata={"ФИО_1": "Петров"},
            is_paid=True,
        )
    )

    response = client.get(
        "/api/wizard/state",
        cookies=_auth_cookie(session_uuid),
        headers={"x-client-fingerprint": "test-fp-3"},
    )
    assert response.status_code == 200

    # Проверим, что в COLD-таблице появилась запись.
    cold = wizard_store._load_cold(uuid.UUID(session_uuid))  # noqa: SLF001
    assert cold is not None
    assert cold.current_stage == "STAGE_3"
    # В COLD-таблице ``final_document_markdown`` пуст — туда едет только
    # обезличенный снимок.
    assert cold.final_document_markdown is None
    # Но ``masking_metadata`` сохраняется (для аудита 152-ФЗ).
    assert cold.masking_metadata == {"ФИО_1": "Петров"}


# ------------------------------------------------------------------------------
# Граничные случаи
# ------------------------------------------------------------------------------
def test_wizard_state_no_document_at_stage_1(client: TestClient):
    """STAGE_1 — нет документа, paywall-логика не запускается."""
    session_uuid = str(uuid.uuid4())
    wizard_store.save_session(
        WizardSession(session_id=uuid.UUID(session_uuid), current_stage="STAGE_1")
    )

    response = client.get(
        "/api/wizard/state",
        cookies=_auth_cookie(session_uuid),
        headers={"x-client-fingerprint": "test-fp-4"},
    )
    assert response.status_code == 200
    data = response.json()
    # final_document_markdown=None на STAGE_1 — paywall не запускается.
    assert data["session"]["final_document_markdown"] is None


def test_wizard_state_no_document_on_stage_3(client: TestClient):
    """STAGE_3 без сгенерированного документа — ответ без 5xx."""
    session_uuid = str(uuid.uuid4())
    wizard_store.save_session(
        WizardSession(
            session_id=uuid.UUID(session_uuid),
            current_stage="STAGE_3",
            final_document_markdown=None,
            is_paid=False,
        )
    )

    response = client.get(
        "/api/wizard/state",
        cookies=_auth_cookie(session_uuid),
        headers={"x-client-fingerprint": "test-fp-5"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["session"]["final_document_markdown"] is None


def test_wizard_state_keeps_paywall_for_empty_metadata(client: TestClient):
    """Пустая ``masking_metadata`` + is_paid=False → paywall-превью."""
    session_uuid = str(uuid.uuid4())
    full_md = "# Заголовок\n\n" + ("Текст. " * 100)
    wizard_store.save_session(
        WizardSession(
            session_id=uuid.UUID(session_uuid),
            current_stage="STAGE_3",
            final_document_markdown=full_md,
            masking_metadata={},
            is_paid=False,
        )
    )

    response = client.get(
        "/api/wizard/state",
        cookies=_auth_cookie(session_uuid),
        headers={"x-client-fingerprint": "test-fp-6"},
    )
    assert response.status_code == 200
    data = response.json()
    md = data["session"]["final_document_markdown"]
    assert md is not None
    assert "<<<PAYWALL_DOCUMENT>>>" in md