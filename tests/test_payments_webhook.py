"""Регрессионные тесты webhook'а Робокассы для Wizard (STAGE_3).

Проверяем ключевые инварианты:

    * эндпоинт ``POST /api/payments/robokassa-webhook`` принимает
      валидный Result URL с подписью ``MD5(OutSum:InvId:Password2)``;
    * неверная подпись → ``HTTP 400 bad sign``;
    * кастомный ``shp_session_id`` корректно связывает платёж с
      :class:`WizardSession` и выставляет ``is_paid=True``;
    * повторное уведомление об уже оплаченном счёте идемпотентно;
    * несуществующий ``shp_session_id`` отклоняется без 5xx;
    * GET-вариант работает по тому же контракту.
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from backend import wizard_store
from backend.config import robokassa_settings
from backend.models import WizardSession


# ------------------------------------------------------------------------------
# Фикстуры
# ------------------------------------------------------------------------------
@pytest.fixture()
def wizard_session() -> WizardSession:
    """Создать чистую Wizard-сессию с маскирующими метаданными."""
    session_id = uuid.uuid4()
    session = WizardSession(
        session_id=session_id,
        current_stage="STAGE_3",
        final_document_markdown=(
            "# Исковое заявление\n\n"
            "Заявитель: [ФИО_1]\nАдрес: [АДРЕС_1]\n"
        ),
        masking_metadata={
            "ФИО_1": "Иванов Иван Иванович",
            "АДРЕС_1": "г. Самара, ул. Ленина, д. 1",
        },
        raw_user_input="",
        is_paid=False,
    )
    wizard_store.save_session(session)
    return session


@pytest.fixture()
def client() -> TestClient:
    """Полноценное приложение для проверки роутера webhook'а."""
    # Импортируем app на каждый тест: conftest.py инициализирует схему БД,
    # поэтому fixture должен подниматься после сброса БД.
    from backend.main import app

    return TestClient(app)


def _valid_signature(out_sum: str, inv_id: str) -> str:
    """Посчитать корректную MD5/SHA256 подпись для Result URL."""
    cfg = robokassa_settings()
    base = f"{out_sum}:{inv_id}:{cfg.password2}"
    return hashlib.new(cfg.hash_algorithm, base.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------
# Сценарии: подпись
# ------------------------------------------------------------------------------
def test_webhook_accepts_valid_signature(
    client: TestClient, wizard_session: WizardSession
) -> None:
    """POST с корректной подписью и ``shp_session_id`` помечает сессию оплаченной."""
    out_sum = "150.00"
    inv_id = "1234567890"
    signature = _valid_signature(out_sum, inv_id)

    response = client.post(
        "/api/payments/robokassa-webhook",
        data={
            "OutSum": out_sum,
            "InvId": inv_id,
            "SignatureValue": signature,
            "shp_session_id": str(wizard_session.session_id),
        },
    )
    assert response.status_code == 200
    assert response.text == f"OK{inv_id}"

    # Сессия помечена как оплаченная на бэкенде.
    updated = wizard_store.load_session(wizard_session.session_id)
    assert updated is not None
    assert updated.is_paid is True
    assert updated.payment_inv_id == inv_id


def test_webhook_rejects_invalid_signature(
    client: TestClient, wizard_session: WizardSession
) -> None:
    """POST с битой подписью → ``HTTP 400 bad sign`` и сессия НЕ помечена."""
    response = client.post(
        "/api/payments/robokassa-webhook",
        data={
            "OutSum": "150.00",
            "InvId": "1234567890",
            "SignatureValue": "0" * 32,  # явно неправильная подпись
            "shp_session_id": str(wizard_session.session_id),
        },
    )
    assert response.status_code == 400
    assert response.text.strip() == "bad sign"

    # Сессия не должна быть помечена как оплаченная.
    updated = wizard_store.load_session(wizard_session.session_id)
    assert updated is not None
    assert updated.is_paid is False


def test_webhook_rejects_missing_shp_session_id(client: TestClient) -> None:
    """POST без ``shp_session_id`` отклоняется даже при валидной подписи."""
    out_sum = "150.00"
    inv_id = "9999"
    signature = _valid_signature(out_sum, inv_id)

    response = client.post(
        "/api/payments/robokassa-webhook",
        data={
            "OutSum": out_sum,
            "InvId": inv_id,
            "SignatureValue": signature,
        },
    )
    assert response.status_code == 400
    assert response.text.strip() == "bad sign"


def test_webhook_rejects_unknown_session(client: TestClient) -> None:
    """POST с несуществующим UUID → ``bad sign`` без 5xx."""
    out_sum = "150.00"
    inv_id = "7777"
    signature = _valid_signature(out_sum, inv_id)

    response = client.post(
        "/api/payments/robokassa-webhook",
        data={
            "OutSum": out_sum,
            "InvId": inv_id,
            "SignatureValue": signature,
            "shp_session_id": str(uuid.uuid4()),  # случайный UUID — сессии нет
        },
    )
    assert response.status_code == 400
    assert response.text.strip() == "bad sign"


def test_webhook_rejects_invalid_uuid_in_shp(client: TestClient) -> None:
    """POST с невалидным UUID → ``bad sign`` без падения 5xx."""
    out_sum = "150.00"
    inv_id = "5555"
    signature = _valid_signature(out_sum, inv_id)

    response = client.post(
        "/api/payments/robokassa-webhook",
        data={
            "OutSum": out_sum,
            "InvId": inv_id,
            "SignatureValue": signature,
            "shp_session_id": "not-a-uuid",
        },
    )
    assert response.status_code == 400
    assert response.text.strip() == "bad sign"


# ------------------------------------------------------------------------------
# Идемпотентность
# ------------------------------------------------------------------------------
def test_webhook_idempotent_on_repeated_call(
    client: TestClient, wizard_session: WizardSession
) -> None:
    """Повторный вызов для того же InvId возвращает ``OK`` без сбоев."""
    out_sum = "150.00"
    inv_id = "1111"
    signature = _valid_signature(out_sum, inv_id)

    # Первый вызов.
    r1 = client.post(
        "/api/payments/robokassa-webhook",
        data={
            "OutSum": out_sum,
            "InvId": inv_id,
            "SignatureValue": signature,
            "shp_session_id": str(wizard_session.session_id),
        },
    )
    assert r1.status_code == 200

    # Второй вызов — тот же.
    r2 = client.post(
        "/api/payments/robokassa-webhook",
        data={
            "OutSum": out_sum,
            "InvId": inv_id,
            "SignatureValue": signature,
            "shp_session_id": str(wizard_session.session_id),
        },
    )
    assert r2.status_code == 200
    assert r2.text == f"OK{inv_id}"

    updated = wizard_store.load_session(wizard_session.session_id)
    assert updated is not None
    assert updated.is_paid is True
    assert updated.payment_inv_id == inv_id


# ------------------------------------------------------------------------------
# GET-вариант
# ------------------------------------------------------------------------------
def test_webhook_get_variant_with_valid_signature(
    client: TestClient, wizard_session: WizardSession
) -> None:
    """GET-вариант webhook'а работает по тому же контракту."""
    out_sum = "195.00"
    inv_id = "2222"
    signature = _valid_signature(out_sum, inv_id)

    response = client.get(
        "/api/payments/robokassa-webhook",
        params={
            "OutSum": out_sum,
            "InvId": inv_id,
            "SignatureValue": signature,
            "shp_session_id": str(wizard_session.session_id),
        },
    )
    assert response.status_code == 200
    assert response.text == f"OK{inv_id}"

    updated = wizard_store.load_session(wizard_session.session_id)
    assert updated is not None
    assert updated.is_paid is True
    assert updated.payment_inv_id == inv_id