"""Wizard API — синхронизация состояний пошагового мастера (Стадия 1).

Два эндпоинта синхронизации:

    * ``GET  /api/wizard/state``  — фронтенд зовёт его при первом монтировании
      SPA, чтобы поднять Wizard на нужной стадии. Если у пользователя ещё
      крутится фоновая задача ``task_id``, эндпоинт пробрасывает её статус,
      и UI продолжает показывать лоадер ровно на той стадии,
    где произошёл обрыв.

    * ``POST /api/wizard/sync-checklist`` — мгновенное сохранение галочек
      чек-листа (STAGE_2). Срабатывает на каждый клик пользователя, поэтому
      обработчик делает ровно один O(1) апдейт hot-кэша и не дёргает БД.

Соглашения:
    * идентификация — через UUID из JWT-cookie (``backend.jwt_auth``);
    * платёжный барьер НЕ включается (визард — это UX-обёртка, она не
      потребляет лимиты);
    * rate-limit IP включён, чтобы ботнет не дёргал ``/state`` тысячу раз.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response

from backend import task_store, wizard_store
from backend.api.deps import enforce_ip_rate_limit
from backend.config import settings
from backend.db import utc_now_iso
from backend.jwt_auth import get_session_from_request
from backend.models import (
    SyncChecklistRequest,
    SyncChecklistResponse,
    WizardSession,
    WizardStage,
    WizardStateResponse,
)

router = APIRouter(prefix="/api/wizard", tags=["wizard"])


def _resolve_session_id(request: Request) -> uuid.UUID:
    """Вернуть UUID сессии пользователя из JWT-cookie.

    Если cookie нет или повреждена, :func:`get_session_from_request` генерирует
    новый UUIDv4 — Wizard стартует с чистого STAGE_1, как для нового визита.
    """
    session_uuid, _ = get_session_from_request(request)
    return uuid.UUID(session_uuid)


def _task_snapshot(task_id: str | None) -> dict[str, Any]:
    """Снимок статуса фоновой задачи (polling). Возвращает пустой dict, если нет."""
    if not task_id:
        return {}
    return task_store.get_task_status(task_id) or {}


# ------------------------------------------------------------------------------
# GET /api/wizard/state
# ------------------------------------------------------------------------------
@router.get("/state", response_model=WizardStateResponse)
async def get_wizard_state(request: Request) -> Response:
    """Восстановить контекст Wizard для фронтенда при загрузке SPA.

    Логика:
        1. Антиабьюз — проверяем IP-rate-limit, иначе ботнет может дёргать
           эндпоинт тысячу раз в минуту и забить Redis «пустыми» ключами.
        2. Достаём UUID из JWT-cookie (см. :func:`_resolve_session_id`).
        3. Сначала ищем в HOT (Redis/memory). Если сессия свежая —
           возвращаем её со всеми заполненными полями.
        4. Если в HOT пусто, идём в COLD (PostgreSQL). Возвращаем
           обезличенный снимок без ``raw_user_input``.
        5. Если и COLD пуст — отдаём чистый STAGE_1.
        6. Если у снимка есть ``task_id`` — опрашиваем ``task_store`` и
           пробрасываем прогресс/стадию в UI (например, чтобы восстановить
           «крутится ли лоадер»).
    """
    enforce_ip_rate_limit(request)

    session_id = _resolve_session_id(request)
    session: WizardSession | None = wizard_store.load_session(session_id)

    if session is None:
        # Первый визит — инициализируем чистый STAGE_1, чтобы UI знал
        # session_id заранее (например, для логов и антифрода).
        session = WizardSession(session_id=session_id)

    snapshot = _task_snapshot(session.task_id)
    storage_mode = "hot" if wizard_store.get_hot_store().get(session_id) else (
        "cold" if session.created_at else "empty"
    )

    body = WizardStateResponse(
        session=session,
        has_active_task=bool(snapshot.get("status") in {"pending", "running"}),
        task_status=snapshot.get("status"),
        task_progress=int(snapshot.get("progress", 0)),
        task_stage=str(snapshot.get("stage", "")),
        storage_mode=storage_mode,
    )
    return Response(
        content=body.model_dump_json(),
        media_type="application/json",
    )


# ------------------------------------------------------------------------------
# POST /api/wizard/sync-checklist
# ------------------------------------------------------------------------------
@router.post("/sync-checklist", response_model=SyncChecklistResponse)
async def sync_wizard_checklist(
    payload: SyncChecklistRequest, request: Request
) -> Response:
    """Мгновенно сохранить состояние галочек чек-листа (STAGE_2).

    Дизайн под polling-стиль клика:

    * при клике по чекбоксу фронт отправляет только изменившийся ключ —
      ``checklist_state`` короткий (``{<id>: true/false}``);
    * обработчик делает ОДИН O(1) апдейт hot-кэша, не трогая COLD (если
      визард не завершён — нет смысла нагружать БД каждым кликом);
    * флаг ``advance_stage`` позволяет синхронно переключить стадию
      (например, после нажатия «Готово» на STAGE_2 — пользователь
      моментально попадает на STAGE_3, не дожидаясь ответа).
    """
    enforce_ip_rate_limit(request)

    session_id = _resolve_session_id(request)
    advance_to: WizardStage | None = "STAGE_3" if payload.advance_stage else None
    updated = wizard_store.patch_checklist(
        session_id,
        delta=payload.checklist_state,
        advance_to=advance_to,
    )
    if updated is None:
        # На случай гонки с GC: сессия исчезла из HOT и COLD между чтениями.
        updated = WizardSession(session_id=session_id)
        updated.checklist_state.update(payload.checklist_state)
        if advance_to is not None:
            updated.current_stage = advance_to
        wizard_store.save_session(updated)

    body = SyncChecklistResponse(
        ok=True,
        saved_keys=sorted(payload.checklist_state.keys()),
        current_stage=updated.current_stage,
        updated_at=updated.updated_at or utc_now_iso(),
    )
    return Response(
        content=body.model_dump_json(),
        media_type="application/json",
        headers={"X-Session-Id": str(session_id)},
    )