"""Pydantic-модели запросов и ответов ClickJurist Production API."""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

ServiceCode = Literal[
    "consultation",
    "checklist",
    "document",
    "pdf",
    "package_basic",
]
DocType = Literal["complaint", "claim", "lawsuit", "court_order_cancellation"]
LegalCategory = Literal["b2b", "b2c"]


class QueryRequest(BaseModel):
    """Запрос на генерацию юридической консультации."""

    query: str = Field(..., min_length=3, description="Описание ситуации клиента")


class ChecklistRequest(BaseModel):
    """Запрос чек-листа по уже полученной консультации."""

    query: str = Field(..., min_length=3)
    final_answer: str = Field(..., min_length=3)


class DocumentRequest(BaseModel):
    """Запрос шаблона документа."""

    query: str = Field(..., min_length=3)
    doc_type: DocType = "lawsuit"


class SourceLink(BaseModel):
    """Проверенный веб-источник, подтверждающий правовую норму."""

    title: str
    url: str


class GenerateResponse(BaseModel):
    """Ответ двухэтапного пайплайна (наружу отдаётся только эталон)."""

    response: str | None = None
    sources: list[SourceLink] = Field(default_factory=list)
    citation_verified: bool = False
    anonymized: bool = True
    stage1_provider: str | None = None
    stage2_provider: str | None = None
    warning: str | None = None
    error: str | None = None
    # Классификатор правовой категории запроса (Шаг 1 ТЗ prompt170926.md).
    # Используется фронтендом для блокировки нерелевантных шаблонов
    # документов в Шаге 3 (Жалоба / Отмена судебного приказа для B2B).
    legal_category: LegalCategory | None = None


class ChecklistResponse(BaseModel):
    """Ответ с чек-листом действий."""

    checklist: str | None = None
    error: str | None = None


class DocumentResponse(BaseModel):
    """Ответ с шаблоном документа."""

    document: str | None = None
    doc_type: str | None = None
    error: str | None = None


class PaymentCreateRequest(BaseModel):
    """Запрос на формирование счёта в Robokassa.

    ``shp_session_id`` — опциональный UUID Wizard-сессии. Если задан,
    пробрасывается Робокассе как кастомный ``shp_session_id`` параметр
    и затем возвращается транзитом в Result URL/webhook, чтобы связать
    платёж с конкретной Wizard-сессией (STAGE_3, раздел 4 ТЗ).
    Сам по себе UUID не содержит ПДн, передача безопасна.
    """

    service: ServiceCode = "consultation"
    shp_session_id: str | None = None


class PaymentCreateResponse(BaseModel):
    """Счёт Robokassa: ссылка на оплату и параметры заказа."""

    inv_id: str
    amount: int
    service: ServiceCode
    payment_url: str
    is_test: bool


class PaymentRequiredResponse(BaseModel):
    """Тело ответа 402 Payment Required (раздел 4 ТЗ)."""

    detail: str
    service: ServiceCode
    amount: int
    payment_url: str
    inv_id: str
    is_test: bool


class SessionStateResponse(BaseModel):
    """Текущее состояние сессии: бесплатный запрос и оплаченный доступ."""

    session_id: str
    is_free: bool
    free_requests_left: int
    paid_access: bool
    paid_until: str | None = None
    prices: dict[str, int] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Состояние сервиса и его контуров."""

    status: str
    environment: str
    masking_contour: str
    analysis_provider: str
    web_search_provider: str
    kms_enabled: bool
    database: str
    storage_mode: str = "No-Data-Retention"


class AsyncTaskResponse(BaseModel):
    """Ответ при постановке задачи в фоновую очередь (раздел 2.1 ТЗ)."""

    task_id: str
    status: str = "pending"
    message: str = "Задача принята в обработку"


class AsyncTaskStatusResponse(BaseModel):
    """Текущий статус фоновой задачи (для polling)."""

    task_id: str
    status: str
    progress: int = 0
    stage: str = ""
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result: dict | None = None
    error: str | None = None


class PackageRequest(BaseModel):
    """Запрос на пакетный тариф «Решение проблемы под ключ» (раздел 6 ТЗ).

    Тариф единственный (``package_basic``, 195 ₽), поэтому параметра выбора
    уровня нет: сервер всегда считает ``amount`` по базовому пакету.
    """

    query: str = Field(..., min_length=3)


class PackageResponse(BaseModel):
    """Результат пакетной генерации (консультация + чек-лист + документ)."""

    consultation: str | None = None
    checklist: str | None = None
    document: str | None = None
    sources: list[SourceLink] = Field(default_factory=list)
    tier: str = "basic"
    amount: int = 0
    warning: str | None = None
    error: str | None = None


# ==============================================================================
# WIZARD: стейт пошагового мастера «КликЮрист» (Стадия 1 — архитектура сессий)
# ==============================================================================
WizardStage = Literal["STAGE_1", "STAGE_2", "STAGE_3"]


class WizardSession(BaseModel):
    """Полный снимок состояния визарда для одного пользователя.

    Схема — единственный источник правды для UI: фронтенд получает её сразу
    при загрузке SPA и поднимает Wizard ровно на той стадии, где пользователь
    остановился. Поля подобраны так, чтобы:

    * восстановление после F5/закрытия вкладки происходило без потерь;
    * в холодное хранилище (PostgreSQL, раздел 4 ТЗ) уезжала только
      обезличенная часть (``raw_user_input`` исключён из cold-снимка);
    * обратная деанонимизация текста на STAGE_3 работала через
      ``masking_metadata`` — карту подстановок «плейсхолдер → реальное ПДн»;
    * факт оплаты через Робокассу (``is_paid``) снимал блюр с документа.

    Структура полей:
        * ``session_id`` — UUIDv4 из JWT-cookie (см. ``backend.jwt_auth``);
        * ``user_id`` — связь с пользователем (опционально, для будущей
          привязки к личному кабинету; сейчас большинство сессий анонимны);
        * ``current_stage`` — текущая фаза визарда на клиенте;
        * ``task_id`` — ID фоновой задачи (``task_store.submit_task``),
          если в момент восстановления крутится пайплайн генерации;
        * ``raw_user_input`` — исходный текст фабулы; хранится ТОЛЬКО в
          Redis (TTL) и НЕ попадает в PostgreSQL — режим No-Data-Retention;
        * ``masking_metadata`` — карта обратной подстановки ПДн для STAGE_3;
        * ``ai_qualification`` — результат STAGE_1 (отрасль, статьи, суть);
        * ``checklist_state`` — JSON-мапа статусов галочек STAGE_2;
        * ``final_document_markdown`` — очищенный Markdown документа STAGE_3;
        * ``is_paid`` — флаг успешной оплаты Робокассой (для снятия блюра);
        * ``timestamps`` — ISO-метки создания/обновления.
    """

    session_id: uuid.UUID
    user_id: int | None = None
    current_stage: WizardStage = "STAGE_1"
    task_id: str | None = None
    raw_user_input: str = ""
    masking_metadata: dict = Field(default_factory=dict)
    ai_qualification: dict = Field(default_factory=dict)
    checklist_state: dict[str, bool] = Field(default_factory=dict)
    final_document_markdown: str | None = None
    is_paid: bool = False
    #: Номер счёта Робокассы (``InvId``), привязанный к успешной оплате.
    #: Заполняется обработчиком вебхука ``/api/payments/robokassa-webhook``
    #: и сохраняется в холодном хранилище (``wizard_cases.payment_inv_id``)
    #: для сверки финансовых документов и спорных платежей.
    payment_inv_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class WizardStateResponse(BaseModel):
    """Снимок состояния, отдаваемый фронтенду при загрузке SPA.

    В отличие от ``WizardSession`` поле ``raw_user_input`` намеренно
    присутствует: фронтенд должен уметь показать пользователю введённый им
    текст на STAGE_1, даже после F5. На STAGE_2/STAGE_3 это поле уже
    заполнено маской и не содержит ПДн. Транспорт — HTTPS, cookie Strict.
    """

    session: WizardSession
    has_active_task: bool = False
    task_status: str | None = None
    task_progress: int = 0
    task_stage: str = ""
    storage_mode: str = "hot-only"  # hot-only | hot-cold для телеметрии


class SyncChecklistRequest(BaseModel):
    """Быстрое сохранение состояния галочек чек-листа (STAGE_2).

    Эндпоинт вызывается СИНХРОННО при каждом клике пользователя по чекбоксу,
    поэтому важно держать тело запроса компактным и не задерживать UI.
    Полный список галочек не передаётся — только изменившийся ключ и его
    новое значение, чтобы не терять такты на сериализацию.
    """

    checklist_state: dict[str, bool] = Field(default_factory=dict)
    advance_stage: bool = False
    """Если ``True`` — синхронно перевести визард на следующую стадию
    (например, когда пользователь нажал «Готово» на STAGE_2)."""


class SyncChecklistResponse(BaseModel):
    """Подтверждение мгновенного сохранения состояния чек-листа."""

    ok: bool = True
    saved_keys: list[str] = Field(default_factory=list)
    current_stage: WizardStage = "STAGE_2"
    updated_at: str = ""
