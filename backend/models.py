"""Pydantic-модели запросов и ответов ClickJurist Production API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ServiceCode = Literal["consultation", "checklist", "document", "pdf"]
DocType = Literal["complaint", "claim", "lawsuit", "court_order_cancellation"]


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
    doc_type: DocType = "isk"


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
    """Запрос на формирование счёта в Robokassa."""

    service: ServiceCode = "consultation"


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
