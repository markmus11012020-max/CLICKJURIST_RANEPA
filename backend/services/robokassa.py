"""Robokassa SDK — платёжный шлюз ClickJurist Production (раздел 4 ТЗ).

Функциональность:
    * :func:`build_payment_url` — формирование ссылки на оплату (sandbox ``test=1``);
    * :func:`verify_result_signature` — проверка подписи Result URL (уведомление);
    * :func:`verify_success_signature` — проверка подписи Success URL (возврат клиента);
    * :func:`create_invoice` — выставление счёта с сохранением в БД.

Алгоритмы подписи (официальная документация Робокассы):
    * Оплата (форма):      ``Hash(Login:OutSum:InvId:Password1[:IsTest])``
    * Success URL:         ``Hash(OutSum:InvId:Password1[:IsTest])``
    * Result URL (оплачен):``Hash(OutSum:InvId:Password2)``

Алгоритм хеширования настраивается в личном кабинете Робокассы
(``ROBOKASSA_HASH_ALGORITHM`` = ``md5`` или ``sha256``).
"""
from __future__ import annotations

import hashlib
import logging
import uuid

from backend.config import RobokassaSettings, robokassa_settings, settings

logger = logging.getLogger("clickjurist.robokassa")


def price_for(service: str) -> int:
    """Вернуть стоимость услуги в рублях (тарифы из конфигурации)."""
    return settings.prices.get(service, settings.PRICE_CONSULTATION)


def _digest(value: str, algorithm: str) -> str:
    """Посчитать хеш подписи выбранным алгоритмом (md5 или sha256)."""
    algo = algorithm.strip().lower()
    try:
        hasher = hashlib.new(algo)
    except ValueError:
        logger.warning("Неизвестный алгоритм подписи '%s' — использую md5", algorithm)
        hasher = hashlib.md5()
    hasher.update(value.encode("utf-8"))
    return hasher.hexdigest()


def generate_inv_id() -> str:
    """Сгенерировать уникальный номер счёта (целое число для Робокассы)."""
    return str(uuid.uuid4().int % 10_000_000_000)


def payment_signature(cfg: RobokassaSettings, out_sum: str, inv_id: str) -> str:
    """Подпись для платёжной формы: ``Login:OutSum:InvId:Password1[:IsTest]``."""
    base = f"{cfg.login}:{out_sum}:{inv_id}:{cfg.password1}"
    if cfg.test:
        base = f"{base}:{cfg.test_param}"
    return _digest(base, cfg.hash_algorithm)


def success_signature(cfg: RobokassaSettings, out_sum: str, inv_id: str) -> str:
    """Подпись возврата клиента: ``OutSum:InvId:Password1[:IsTest]``."""
    base = f"{out_sum}:{inv_id}:{cfg.password1}"
    if cfg.test:
        base = f"{base}:{cfg.test_param}"
    return _digest(base, cfg.hash_algorithm)


def result_signature(cfg: RobokassaSettings, out_sum: str, inv_id: str) -> str:
    """Подпись уведомления об оплате: ``OutSum:InvId:Password2``."""
    return _digest(f"{out_sum}:{inv_id}:{cfg.password2}", cfg.hash_algorithm)


def build_payment_url(
    inv_id: str, amount: int, service: str, description: str = ""
) -> str:
    """Собрать ссылку на оплату с корректными параметрами Робокассы."""
    cfg = robokassa_settings()
    out_sum = f"{amount:.2f}"
    signature = payment_signature(cfg, out_sum, inv_id)

    params = {
        "MerchantLogin": cfg.login,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": description or f"ClickJurist: услуга «{service}»",
        "SignatureValue": signature,
        "Culture": "ru",
    }
    if cfg.test:
        params["IsTest"] = str(cfg.test_param)
    else:
        params["SuccessUrl2"] = cfg.absolute(cfg.success_url)
        params["FailUrl2"] = cfg.absolute(cfg.fail_url)

    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{cfg.payment_url}?{query}"


def verify_result_signature(out_sum: str, inv_id: str, signature: str) -> bool:
    """Проверить подпись уведомления Result URL (constant-time сравнение)."""
    cfg = robokassa_settings()
    expected = result_signature(cfg, out_sum, inv_id).lower()
    received = (signature or "").strip().lower()
    return _constant_time_equal(expected, received)


def verify_success_signature(out_sum: str, inv_id: str, signature: str) -> bool:
    """Проверить подпись возврата Success URL."""
    cfg = robokassa_settings()
    expected = success_signature(cfg, out_sum, inv_id).lower()
    received = (signature or "").strip().lower()
    return _constant_time_equal(expected, received)


def _constant_time_equal(left: str, right: str) -> bool:
    """Сравнение строк за постоянное время (защита от timing-атак)."""
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def result_ack(inv_id: str) -> str:
    """Обязательный ответ Робокассе на Result URL: ``OK<InvId>``."""
    return f"OK{inv_id}"


def create_invoice(service: str, session_hash: str) -> dict[str, object]:
    """Выставить счёт: сохранить платёж в БД и собрать ссылку на оплату.

    Args:
        service: код услуги (``consultation`` | ``checklist`` | ``document`` | ``pdf``).
        session_hash: псевдонимизированный хеш сессии (152-ФЗ).

    Returns:
        Словарь с параметрами счёта для передачи фронтенду.
    """
    from backend.db import store  # локальный импорт во избежание циклов

    cfg = robokassa_settings()
    amount = price_for(service)
    inv_id = generate_inv_id()
    store.create_payment(inv_id, session_hash, service, amount)
    payment_url = build_payment_url(inv_id, amount, service)

    logger.info(
        "Счёт выставлен",
        extra={"provider": "robokassa", "session_id": session_hash[:16]},
    )
    return {
        "inv_id": inv_id,
        "amount": amount,
        "service": service,
        "payment_url": payment_url,
        "is_test": cfg.test,
    }


def confirm_payment(out_sum: str, inv_id: str, signature: str) -> tuple[bool, str]:
    """Подтвердить оплату по уведомлению Result URL.

    Returns:
        Кортеж ``(успех, текст_ответа_робокассе)``.
    """
    from backend.db import store

    if not verify_result_signature(out_sum, inv_id, signature):
        logger.warning(
            "Неверная подпись Result URL — платёж отклонён",
            extra={"provider": "robokassa"},
        )
        return False, "bad signature"

    payment = store.get_payment(inv_id)
    if payment is None:
        logger.warning(
            "Платёж с указанным InvId не найден",
            extra={"provider": "robokassa"},
        )
        return False, "invoice not found"

    if str(payment.get("status")) == "paid":
        # Идемпотентность: повторное уведомление не меняет состояние
        return True, result_ack(inv_id)

    store.mark_payment_paid(inv_id)
    store.grant_paid_access(str(payment["session_hash"]))
    logger.info(
        "Платёж подтверждён",
        extra={"provider": "robokassa", "stage": "payment"},
    )
    return True, result_ack(inv_id)


def fail_payment(inv_id: str) -> None:
    """Отметить платёж неуспешным при возврате по Fail URL."""
    from backend.db import store

    store.mark_payment_failed(inv_id)