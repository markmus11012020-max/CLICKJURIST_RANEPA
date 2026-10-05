"""Оплата через Robokassa (раздел 4 ТЗ)."""
from __future__ import annotations

import hashlib
import time
import traceback

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backend.config import settings
from backend.db import store
from backend.logging_setup import setup_logging
from backend.models import PaymentCreateRequest, PaymentCreateResponse
from backend.security import session_context
from backend.services import robokassa

logger = setup_logging()

router = APIRouter(prefix="/api/payment", tags=["payment"])

#: Значения из .env.example — считаем, что Робокassa не настроена.
_ROBOKASSA_DEFAULTS = {"clickjurist", "test_password_1", "test_password_2"}


def robokassa_keys_present() -> bool:
    """Проверить, что ключи Робокассы заданы (не пустые и не дефолтные)."""
    login = (settings.ROBOKASSA_LOGIN or "").strip()
    pwd1 = (settings.ROBOKASSA_PASSWORD1 or "").strip()
    pwd2 = (settings.ROBOKASSA_PASSWORD2 or "").strip()
    if not login or not pwd1 or not pwd2:
        return False
    return {login, pwd1, pwd2} > _ROBOKASSA_DEFAULTS


def build_robokassa_url(
    inv_id: str, amount: int, service: str, description: str = ""
) -> str:
    """Собрать реальный URL Робокассы с MD5-подписью.

    Формула подписи (официальная документация Robokassa):
        SignatureValue = MD5(MerchantLogin:OutSum:InvId:Password1)

    Параметры читаются из окружения с безопасным fallback на плейсхолдеры,
    чтобы функция не падала при локальной отладке без реальных ключей.
    """
    login = (settings.ROBOKASSA_LOGIN or "demo_login").strip() or "demo_login"
    password1 = (
        settings.ROBOKASSA_PASSWORD1 or "demo_password_1"
    ).strip() or "demo_password_1"
    is_test = bool(settings.ROBOKASSA_TEST)

    out_sum = f"{amount:.2f}"
    signature = hashlib.md5(
        f"{login}:{out_sum}:{inv_id}:{password1}".encode()
    ).hexdigest()

    desc = description or f"ClickJurist: услуга «{service}»"
    base_url = (
        settings.ROBOKASSA_PAYMENT_URL
        or "https://auth.robokassa.ru/Merchant/Index.aspx"
    )
    params = (
        f"MerchantLogin={login}"
        f"&OutSum={out_sum}"
        f"&InvId={inv_id}"
        f"&Description={desc}"
        f"&SignatureValue={signature}"
        f"&IsTest={1 if is_test else 0}"
    )
    return f"{base_url}?{params}"


def mock_invoice(service: str, session_hash: str) -> dict[str, object]:
    """Сгенерировать фейковый счёт для локального тестирования без ключей."""
    amount = settings.prices.get(service, settings.PRICE_CONSULTATION)
    inv_id = f"MOCK-{int(time.time() * 1000)}"
    logger.warning(
        "Robokassa keys missing/empty — используем MOCK-режим (service=%s, amount=%s)",
        service,
        amount,
    )
    return {
        "inv_id": inv_id,
        "amount": amount,
        "service": service,
        "payment_url": build_robokassa_url(inv_id, amount, service),
        "is_test": True,
    }


# ------------------------------------------------------------------------------
# Создание счёта
# ------------------------------------------------------------------------------
@router.post("/create", response_model=PaymentCreateResponse)
async def api_payment_create(
    payload: PaymentCreateRequest, request: Request
) -> PaymentCreateResponse:
    """Выставить счёт на оплату выбранной услуги (sandbox: IsTest=1)."""
    try:
        session_hash, _session_id, _ip = session_context(request)
        store.ensure_session(session_hash)

        # Fallback: если ключи Робокассы не настроены — отдаём mock-ссылку,
        # чтобы локальная отладка не падала с HTTP 500.
        if not robokassa_keys_present():
            invoice = mock_invoice(payload.service, session_hash)
        else:
            invoice = robokassa.create_invoice(payload.service, session_hash)

        return PaymentCreateResponse(**invoice)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Ошибка в /api/payment/create: %s\n%s",
            exc,
            traceback.format_exc(),
        )
        # Возвращаем mock-ссылку вместо 500, чтобы UI не падал.
        try:
            amount = settings.prices.get(payload.service, settings.PRICE_CONSULTATION)
        except Exception:
            amount = 0
        inv_id = f"ERR-{int(time.time() * 1000)}"
        return PaymentCreateResponse(
            inv_id=inv_id,
            amount=amount,
            service=payload.service,
            payment_url=build_robokassa_url(inv_id, amount, payload.service),
            is_test=True,
        )


# ------------------------------------------------------------------------------
# Возврат и серверное уведомление
# ------------------------------------------------------------------------------
@router.api_route("/result", methods=["GET", "POST"])
async def api_payment_result(request: Request) -> Response:
    """Result URL: серверное уведомление Робокассы об успешной оплате.

    Ответ строго ``OK<InvId>`` — иначе Робокасса повторяет уведомление.
    """
    params: dict[str, str] = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({key: str(value) for key, value in form.items()})

    ok, message = robokassa.confirm_payment(
        str(params.get("OutSum", "")),
        str(params.get("InvId", "")),
        str(params.get("SignatureValue", "")),
    )
    return Response(
        content=message, media_type="text/plain", status_code=200 if ok else 400
    )


@router.get("/success")
async def api_payment_success(request: Request) -> Response:
    """Success URL: возврат пользователя после успешной оплаты."""
    params = request.query_params
    verified = robokassa.verify_success_signature(
        str(params.get("OutSum", "")),
        str(params.get("InvId", "")),
        str(params.get("SignatureValue", "")),
    )
    return Response(
        content=payment_result_page(
            success=True, inv_id=str(params.get("InvId", "")), verified=verified
        ),
        media_type="text/html; charset=utf-8",
    )


@router.get("/fail")
async def api_payment_fail(request: Request) -> Response:
    """Fail URL: возврат пользователя при отказе от оплаты."""
    inv_id = str(request.query_params.get("InvId", ""))
    if inv_id:
        robokassa.fail_payment(inv_id)
    return Response(
        content=payment_result_page(success=False, inv_id=inv_id, verified=False),
        media_type="text/html; charset=utf-8",
    )


def payment_result_page(success: bool, inv_id: str, verified: bool) -> str:
    """Страница возврата с оплаты (без внешних зависимостей и ПДн)."""
    if success and verified:
        title = "Оплата подтверждена"
        text = (
            "Доступ к платным функциям ClickJurist активирован. "
            "Вернитесь в сервис и продолжите работу."
        )
    elif success:
        title = "Платёж обрабатывается"
        text = (
            "Подпись возврата не совпала, но если оплата прошла — доступ "
            "активируется автоматически после уведомления Робокассы."
        )
    else:
        title = "Оплата не завершена"
        text = "Вы можете вернуться в сервис и попробовать оплатить снова."

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} — ClickJurist</title>
  <style>
    body {{ margin:0; min-height:100vh; display:flex; align-items:center;
            justify-content:center; background:#0b1020; color:#e8ecf7;
            font-family:'Segoe UI', system-ui, sans-serif; }}
    .card {{ max-width:520px; padding:40px; border-radius:20px;
             background:rgba(255,255,255,.06);
             border:1px solid rgba(255,255,255,.12); text-align:center; }}
    h1 {{ font-size:26px; margin:0 0 12px; }}
    p {{ color:#b6c2e2; line-height:1.6; }}
    .inv {{ font-size:13px; color:#7f8db3; margin-top:18px; }}
    a {{ display:inline-block; margin-top:24px; padding:12px 26px;
         border-radius:12px; background:linear-gradient(135deg, #818cf8 0%, #4f46e5 100%);
         color:#ffffff; text-decoration:none; font-weight:600;
         box-shadow:0 10px 24px -12px rgba(99,102,241,.55); }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{text}</p>
    <div class="inv">Счёт № {inv_id or '—'}</div>
    <a href="/">Вернуться в ClickJurist</a>
  </div>
</body>
</html>"""
