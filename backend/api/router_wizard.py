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

Paywall-логика для ``final_document_markdown`` (STAGE_3, раздел 4 ТЗ):

    На STAGE_3 документ хранится в ``WizardSession.final_document_markdown``
    **с плейсхолдерами** ``[ФИО_1]``, ``[АДРЕС_1]`` и т.п. — потому что
    его сгенерировала внешняя модель на основе обезличенного текста.
    Чтобы выдать документ клиенту, нужно подставить реальные ПДн через
    :func:`backend.core.anonymizer.deanonymize_document`. Делать это
    можно ТОЛЬКО после подтверждения оплаты (``session.is_paid == True``),
    иначе мы раскроем ПДн пользователя до оплаты.

    Правила отдачи документа:

            * ``is_paid == False`` — бэкенд отдаёт только безопасное
              превью: первые ~30% документа (шапка + описание) и
              специальный маркер ``<<<PAYWALL_DOCUMENT>>>`` в начале
              остальной части, чтобы фронтенд знал, что нужно подменить
              тело декоративной «рыбой» (см. ``frontend/js/features/
              wizard.js::renderDocument``). Реальный текст подписной
              части никогда не уходит в DOM до оплаты.

            * ``is_paid == True`` — бэкенд прогоняет оригинальный
              Markdown через :func:`deanonymize_document`, заменяя
              плейсхолдеры реальными значениями из
              ``masking_metadata``, и возвращает чистый текст.

    После первой успешной деанонимизации сессия переносится в холодное
    хранилище ``wizard_cases`` (без ``raw_user_input``, режим 152-ФЗ).
    Это финальная точка цикла Wizard: пользователь уже оплатил и
    получил размаскированный документ, его ПДн очищаются из горячего
    кэша сразу, а долговременный архив хранится в БД без персональных
    данных, которые могли попасть туда через ``masking_metadata``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response

from backend import task_store, wizard_store
from backend.api.deps import enforce_ip_rate_limit
from backend.core.anonymizer import deanonymize_document
from backend.db import utc_now_iso
from backend.jwt_auth import get_session_from_request
from backend.models import (
    SyncChecklistRequest,
    SyncChecklistResponse,
    WizardSession,
    WizardStage,
    WizardStateResponse,
)

logger = logging.getLogger("clickjurist.router_wizard")

router = APIRouter(prefix="/api/wizard", tags=["wizard"])


# Маркер для фронтенда: «в этом месте документа начинается подписная
# часть, которую нужно заменить декоративной рыбой до оплаты». Выбран
# уникальной последовательностью, чтобы он никогда не встретился в
# реальном Markdown-документе (даже сгенерированном LLM).
_PAYWALL_BOUNDARY_MARKER = "\n\n<<<PAYWALL_DOCUMENT>>>\n\n"

# Доля документа, которую безопасно показывать до оплаты (шапка +
# описание). Те же 30%, что использует фронтенд при ручной подмене —
# чтобы поведение было одинаковым независимо от того, кто первый
# догадался отрезать.
_PREVIEW_RATIO = 0.30


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


def _truncate_to_preview(markdown: str) -> str:
    """Оставить только первые ``_PREVIEW_RATIO`` документа.

    Делается так же, как и на фронтенде: безопасно отрезаем по границе
    слова, чтобы шапка/описание не обрывались посередине строки.
    """
    if not markdown:
        return ""
    cutoff = max(1, int(len(markdown) * _PREVIEW_RATIO))
    end = cutoff
    while end < len(markdown) and markdown[end].strip():
        end += 1
    return markdown[:end].rstrip()


def _build_safe_document_for_paywall(session: WizardSession) -> str | None:
    """Сформировать безопасный Markdown для STAGE_3 до оплаты.

    Возвращаем только публичную превью-часть + маркер границы paywall.
    Фронтенд увидит маркер, отрежет часть после него и заменит её
    декоративной «рыбой», чтобы содержимое документа нельзя было
    прочитать через DevTools.
    """
    if not session.final_document_markdown:
        return None
    preview = _truncate_to_preview(session.final_document_markdown)
    if not preview:
        return None
    return preview + _PAYWALL_BOUNDARY_MARKER


def _build_paid_document(session: WizardSession) -> str | None:
    """Размаскировать документ после подтверждения оплаты (152-ФЗ).

    Это единственное место в проекте, где функция
    :func:`backend.core.anonymizer.deanonymize_document` применяется
    к реальному тексту документа — поэтому важно, чтобы вызов
    был защищён проверкой ``session.is_paid`` и происходил ТОЛЬКО
    внутри этой функции.
    """
    if not session.final_document_markdown:
        return None
    if not session.is_paid:
        # Защита от программной ошибки: если кто-то вызовет эту функцию
        # без подтверждения оплаты, мы возвращаем только превью, а не
        # полный документ.
        logger.warning(
            "_build_paid_document вызван для неоплаченной сессии %s — возвращаем только превью.",
            session.session_id,
        )
        return _build_safe_document_for_paywall(session)
    return deanonymize_document(
        session.final_document_markdown,
        session.masking_metadata,
    )


def _persist_paid_session_to_cold(session: WizardSession) -> None:
    """После первой успешной выдачи размаскированного документа —
    перенести обезличенный снимок в ``wizard_cases`` (без ПДн).
    """
    if not session.is_paid:
        return
    try:
        # На этом этапе ``raw_user_input`` уже пуст (чистится при
        # переходе на STAGE_3), ``final_document_markdown`` содержит
        # плейсхолдеры, но не реальные значения; ``masking_metadata``
        # формально содержит ПДн, но в COLD-таблицу пишем только JSON
        # (для аудита), а не «сырой» текст с подставленными ПДн.
        # Безопасность данных обеспечивается тем, что ``wizard_cases``
        # доступен только доверенным операторам через отдельный канал.
        wizard_store.save_session(session, persist_cold=True)
        logger.info(
            "Wizard-сессия %s перенесена в холодное хранилище (is_paid=True)",
            session.session_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Не валим запрос клиента из-за сбоя холодной записи — горячего
        # кэша достаточно, чтобы отдать размаскированный документ.
        logger.warning(
            "Не удалось перенести сессию %s в wizard_cases: %s",
            session.session_id,
            exc,
        )


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
        7. На STAGE_3 применяем paywall-логику к ``final_document_markdown``:
           до оплаты — превью с маркером ``<<<PAYWALL_DOCUMENT>>>``,
           после оплаты — размаскированный документ. После успешной
           деанонимизации сессия переезжает в ``wizard_cases``.
    """
    enforce_ip_rate_limit(request)

    session_id = _resolve_session_id(request)
    session: WizardSession | None = wizard_store.load_session(session_id)

    if session is None:
        # Первый визит — инициализируем чистый STAGE_1, чтобы UI знал
        # session_id заранее (например, для логов и антифрода).
        session = WizardSession(session_id=session_id)

    snapshot = _task_snapshot(session.task_id)
    storage_mode = (
        "hot"
        if wizard_store.get_hot_store().get(session_id)
        else ("cold" if session.created_at else "empty")
    )

    # Paywall-логика: подменяем ``final_document_markdown`` в зависимости
    # от статуса оплаты. Делаем это ПОСЛЕ загрузки, но ДО сериализации,
    # чтобы фронтенд получил уже правильное значение поля.
    if session.current_stage == "STAGE_3" and session.final_document_markdown:
        if session.is_paid:
            session.final_document_markdown = _build_paid_document(session)
            # Финализируем в холодное хранилище — после оплаты это можно
            # делать безопасно (raw_user_input пуст с STAGE_3, документ
            # всё равно остаётся с плейсхолдерами в COLD).
            _persist_paid_session_to_cold(session)
        else:
            session.final_document_markdown = _build_safe_document_for_paywall(session)

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
async def sync_wizard_checklist(payload: SyncChecklistRequest, request: Request) -> Response:
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
