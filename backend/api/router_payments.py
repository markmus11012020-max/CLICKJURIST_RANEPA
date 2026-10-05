"""Webhook-эндпоинт Робокассы для Wizard-сценария (раздел 4 ТЗ).

Зачем нужен отдельный роутер:

    Существующий ``backend.api.router_payment`` обслуживает «общий» путь
    оплаты услуг (консультация / чек-лист / документ / PDF / пакет) и
    работает через сервис :mod:`backend.services.robokassa` с
    псевдонимизированным ``session_hash``. Для Wizard-сценария
    (``backend.wizard_store``) нужен отдельный путь, потому что:

        * Робокасса возвращает кастомные ``shp_*`` параметры — мы
          передаём ``shp_session_id`` (UUID Wizard-сессии) при выставлении
          счёта, и по нему однозначно находим нужный :class:`WizardSession`;
        * платёж должен обновить ``is_paid`` и ``payment_inv_id`` именно
          в Wizard-сессии, а не в общем счётчике ``sessions`` (который
          используется для бесплатных лимитов и обычных платных услуг);
        * после успешной верификации сессия переносится в холодное
          хранилище ``wizard_cases`` (без ``raw_user_input``, 152-ФЗ).

Протокол Робокассы (Result URL):

    Робокасса присылает ``POST`` (или ``GET``) с параметрами::

            OutSum    = 195.00
            InvId     = 1234567890
            SignatureValue = <md5/sha256(OutSum:InvId:Password2)>
            shp_session_id = <UUID>
            ...

    Корректный ответ — строка ``OK<InvId>``. При неверной подписи или
    нераспознанном счёте — HTTP 400 с телом ``bad sign`` / ``bad
    invoice``. Робокасса повторяет уведомление, пока не получит ``OK``
    в течение ограниченного времени, поэтому ответ должен быть строго
    синхронным.

Безопасность:

    * Подпись проверяется через :func:`backend.services.robokassa.
      verify_result_signature` (constant-time сравнение через ``hmac``).
    * Любые ошибки валидации/БД логируются, но клиенту (робокассе)
      возвращается нейтральный ответ ``bad sign`` — утечки внутренних
      деталей наружу быть не должно.
    * Никакого traceback/exception в тексте ответа робокассе — только
      согласованные статусы.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from backend import wizard_store
from backend.logging_setup import setup_logging
from backend.models import WizardSession
from backend.services import robokassa

logger = setup_logging()

router = APIRouter(prefix="/api/payments", tags=["payments-webhook"])


def _bad_response(reason: str) -> Response:
    """Сформировать «отказ» для Робокассы — текст/plain, HTTP 400.

    Используем именно ``text/plain`` и HTTP 400, чтобы Робокасса
    распознала сценарий ошибки и повторила уведомление, если дело в
    сетевой гонке. Для безопасности НЕ раскрываем ``reason`` наружу:
    внутри логов оставляем подробности, наружу — общий код.
    """
    # Внутренний лог — для расследования инцидентов.
    logger.warning("Robokassa webhook отклонён: %s", reason)
    return Response(
        content="bad sign",
        status_code=400,
        media_type="text/plain; charset=utf-8",
    )


def _ok_response(inv_id: str) -> Response:
    """Корректный ответ Робокассе: ``OK<InvId>``, HTTP 200."""
    return Response(
        content=f"OK{inv_id}",
        status_code=200,
        media_type="text/plain; charset=utf-8",
    )


def _coerce_form_value(value: Any) -> str:
    """Привести значение формы к строке (Robokassa иногда шлёт ``bytes``)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1", errors="replace")
    return str(value)


@router.post("/robokassa-webhook")
async def robokassa_webhook(
    request: Request,
    OutSum: str = Form("", alias="OutSum"),
    InvId: str = Form("", alias="InvId"),
    SignatureValue: str = Form("", alias="SignatureValue"),
    shp_session_id: str | None = Form(None, alias="shp_session_id"),
) -> Response:
    """Асинхронный webhook Робокассы для Wizard-сценария.

    Ожидаемые поля:
        ``OutSum`` — сумма платежа (``195.00``);
        ``InvId`` — номер счёта Робокассы;
        ``SignatureValue`` — подпись ``MD5/SHA256(OutSum:InvId:Password2)``;
        ``shp_session_id`` — UUID Wizard-сессии (``custom_shp``).

    Логика:

        1. Проверить подпись через :func:`verify_result_signature`.
        2. Распарсить ``shp_session_id`` в UUID. Без него — отказ.
        3. Загрузить :class:`WizardSession` из ``wizard_store``.
           Если сессии нет — отказ (это мог быть Robo-kin если
           произошла слишком долгая задержка между выставлением
           счёта и оплатой; в этом случае мы ничего не делаем).
        4. Выставить ``is_paid=True`` и ``payment_inv_id=InvId``.
        5. Сохранить сессию в горячем кэше. Холодную запись делает
           вызывающая сторона (эндпоинт ``/api/wizard/state``) — там
           же происходит финальная деанонимизация документа, чтобы
           не дублировать бизнес-логику между двумя эндпоинтами.

    Возвращает корректный ответ Робокассе (``OK<InvId>`` или ``bad sign``).
    """
    try:
        # 1. Подпись — критическая проверка. Делаем её ДО любых обращений
        #    к БД и wizard_store: иначе при неверной подписи злоумышленник
        #    мог бы «пробивать» UUID-ы сессий через ответ 404/200.
        if not OutSum or not InvId or not SignatureValue:
            return _bad_response("missing required fields")

        if not robokassa.verify_result_signature(OutSum, InvId, SignatureValue):
            return _bad_response("invalid signature")

        # 2. shp_session_id — обязателен для Wizard-сценария.
        if not shp_session_id:
            return _bad_response("missing shp_session_id")

        try:
            session_uuid = uuid.UUID(str(shp_session_id).strip())
        except (ValueError, TypeError, AttributeError):
            return _bad_response("invalid shp_session_id")

        # 3. Загружаем сессию. Если её нет — пользователь, скорее всего,
        #    закрыл вкладку до оплаты, и TTL «горячего» кэша истёк. Это
        #    нормальная ситуация: отказываем Робокассе без шума, но и без
        #    5xx, чтобы она не повторяла уведомление бесконечно.
        session: WizardSession | None = wizard_store.load_session(session_uuid)
        if session is None:
            logger.info(
                "Robokassa webhook: Wizard-сессия %s не найдена "
                "(TTL истёк или пользователь закрыл вкладку).",
                session_uuid,
            )
            return _bad_response("wizard session not found")

        # 4. Идемпотентность: повторное уведомление об уже оплаченном
        #    счёте не должно сбрасывать дату/содержимое.
        if session.is_paid and session.payment_inv_id == InvId:
            return _ok_response(InvId)

        session.is_paid = True
        session.payment_inv_id = InvId
        # Холодную запись оставим для ``/api/wizard/state``: только там
        # понятно, что документ уже построен и можно его финализировать.
        # На этом этапе мы просто фиксируем факт оплаты.
        wizard_store.save_session(session, persist_cold=False)

        logger.info(
            "Wizard-сессия %s помечена как оплаченная (InvId=%s)",
            session_uuid,
            InvId,
        )
        return _ok_response(InvId)

    except Exception as exc:  # noqa: BLE001 — широкий catch: логируем + отказ
        logger.error(
            "Необработанная ошибка в /api/payments/robokassa-webhook: %s",
            exc,
            exc_info=True,
        )
        return _bad_response("internal error")


@router.get("/robokassa-webhook")
async def robokassa_webhook_get(request: Request) -> Response:
    """GET-вариант webhook'а: некоторые интеграции Робокассы присылают GET.

    Контракт строго тот же — те же параметры в query-string, та же
    верификация подписи. Реализация делегирует основную логику функции
    :func:`robokassa_webhook` через повторный POST не получится —
    проще принять GET-вариант напрямую.
    """
    try:
        params = dict(request.query_params)
        out_sum = _coerce_form_value(params.get("OutSum", ""))
        inv_id = _coerce_form_value(params.get("InvId", ""))
        signature = _coerce_form_value(params.get("SignatureValue", ""))
        shp_session_id = _coerce_form_value(params.get("shp_session_id", "")) or None

        if not out_sum or not inv_id or not signature:
            return _bad_response("missing required fields")

        if not robokassa.verify_result_signature(out_sum, inv_id, signature):
            return _bad_response("invalid signature")

        if not shp_session_id:
            return _bad_response("missing shp_session_id")

        try:
            session_uuid = uuid.UUID(str(shp_session_id).strip())
        except (ValueError, TypeError, AttributeError):
            return _bad_response("invalid shp_session_id")

        session = wizard_store.load_session(session_uuid)
        if session is None:
            logger.info(
                "Robokassa webhook (GET): Wizard-сессия %s не найдена.",
                session_uuid,
            )
            return _bad_response("wizard session not found")

        if session.is_paid and session.payment_inv_id == inv_id:
            return _ok_response(inv_id)

        session.is_paid = True
        session.payment_inv_id = inv_id
        wizard_store.save_session(session, persist_cold=False)

        logger.info(
            "Wizard-сессия %s помечена как оплаченная (GET, InvId=%s)",
            session_uuid,
            inv_id,
        )
        return _ok_response(inv_id)

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Необработанная ошибка в GET /api/payments/robokassa-webhook: %s",
            exc,
            exc_info=True,
        )
        return _bad_response("internal error")


# Экспортируемый список (для unit-тестов и для явно, не проверки).
__all__ = ["router", "robokassa_webhook"]
