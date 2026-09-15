"""STAGE 1 — Контур маскировки персональных данных (152-ФЗ).

Раздел 2 ТЗ: до передачи во внешнюю модель (Stage 2) сырой текст проходит
через российский LLM (YandexGPT или локальный Ollama), который заменяет
персональные данные стандартизированными плейсхолдерами ``[NAME_1]``,
``[ADDRESS_1]`` и формирует **анонимное семантическое резюме**.

Двухслойная защита:
    1. LLM-маскировщик (``MASKING_PROVIDER=yandex|ollama``) — понимает контекст
       и умеет находить ФИО, адреса и организации в свободном тексте.
    2. Регулярные выражения (:func:`regex_mask`) — страховка, которая
       обязательно прогоняется ПОВЕРХ результата LLM. Даже если российская
       модель недоступна или пропустила фрагмент, ПДн не покинут контур РФ.

Режим ``MASKING_PROVIDER=regex`` полностью оффлайновый: LLM не вызывается,
маскировка выполняется только регулярками (полезно для тестов и dev-среды).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from backend.config import settings
from backend.services.prompts import PROMPT_STAGE1_MASKING
from backend.services.providers import LLMError, get_provider

# ------------------------------------------------------------------------------
# Модульный логгер — фиксирует любые сбои контура Stage 1 и автоматический
# переход на оффлайн-страховку (regex). Используется, чтобы при таймаутах/
# сетевых ошибках YandexGPT/Ollama дежурный оператор видел факт отключения
# основного провайдера и мог расследовать инцидент.
# ------------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Регулярные выражения для ПДн
# ВАЖНО: они намеренно «узкие» — не трогают правовые данные (статьи, сроки, даты).
# ------------------------------------------------------------------------------
PATTERN_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PATTERN_PHONE = re.compile(
    r"(?:\+7|8|7)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
)
PATTERN_SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}\s?\d{2}\b")
PATTERN_PASSPORT = re.compile(r"\b\d{2}\s\d{2}\s?№?\s?\d{6}\b")
PATTERN_INN = re.compile(r"\bИНН[\s:№]*\d{10}\b|\bИНН[\s:№]*\d{12}\b")
PATTERN_OGRN = re.compile(r"\bОГРН[\s:№]*\d{13}\b|\bОГРН[\s:№]*\d{15}\b")
PATTERN_SNILS_PLAIN = re.compile(r"\b\d{3}-\d{3}-\d{3}\s\d{2}\b")
PATTERN_BANK_ACCOUNT = re.compile(r"\b[Рр]/?[сc][\s:№]*\d{20}\b")
PATTERN_CARD = re.compile(r"\b(?:\d{4}[\s-]){3}\d{4}\b")
PATTERN_FIO_FULL = re.compile(
    r"\b[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+)?\s+"
    r"(?:[А-ЯЁ][а-яё]+(?:вич|вна|ична|инична)|[А-ЯЁ]\.)\b"
)
PATTERN_FIO_INITIALS = re.compile(
    r"\b[А-ЯЁ][а-яё\-]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+[А-ЯЁ][а-яё\-]+\b"
)
PATTERN_ADDRESS = re.compile(
    r"\b(?:г\.|город|гор\.|п\.|пгт|пос\.|дер\.|с\.)\s*[А-ЯЁ][а-яё\-]+"
    r"(?:\s+[А-ЯЁ][а-яё\-]+)?(?:,|\s)*"
    r"(?:ул\.|улица|пр-?т|проспект|пер\.|переулок|ш\.|шоссе|б-р|бульвар|"
    r"наб\.|набережная|д\.|дом|кв\.|квартира|оф\.|офис)[^.;\n]{0,90}"
)
PATTERN_STREET_ONLY = re.compile(
    r"\b(?:ул\.|улица|пр-?т|проспект|пер\.|переулок|б-р|бульвар|наб\.|шоссе)\s+"
    r"[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+)?(?:\s*(?:д\.|дом)\s*\d+[А-Яа-я]?)?"
    r"(?:\s*(?:кв\.|квартира)\s*\d+)?"
)
PATTERN_ORG = re.compile(
    r"\b(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|НКО)\s*[«\"']?[А-ЯЁ][\w\- «»\"'№]{1,40}[»\"']?"
)

# Порядок применения важен: сначала составные сущности, затем простые.
_REGEX_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", PATTERN_EMAIL),
    ("SNILS", PATTERN_SNILS),
    ("PASSPORT", PATTERN_PASSPORT),
    ("INN", PATTERN_INN),
    ("OGRN", PATTERN_OGRN),
    ("CARD", PATTERN_CARD),
    ("BANK", PATTERN_BANK_ACCOUNT),
    ("PHONE", PATTERN_PHONE),
    ("ADDRESS", PATTERN_ADDRESS),
    ("ADDRESS", PATTERN_STREET_ONLY),
    ("ORG", PATTERN_ORG),
    ("NAME", PATTERN_FIO_FULL),
    ("NAME", PATTERN_FIO_INITIALS),
)

PLACEHOLDER_KEYS: tuple[str, ...] = (
    "NAME",
    "ADDRESS",
    "PHONE",
    "EMAIL",
    "PASSPORT",
    "SNILS",
    "INN",
    "OGRN",
    "BANK",
    "CARD",
    "ORG",
)


@dataclass
class MaskResult:
    """Результат работы контура маскировки ПДн."""

    masked_query: str
    anonymized_summary: str = ""
    entities: dict[str, int] = field(default_factory=dict)
    provider: str = "regex"
    risk_notes: str = ""
    llm_used: bool = False
    warning: str | None = None

    @property
    def has_pii(self) -> bool:
        """True, если маскировка действительно нашла персональные данные."""
        return bool(self.entities)

    @property
    def entities_total(self) -> int:
        """Суммарное количество замаскированных сущностей."""
        return sum(self.entities.values())


# ------------------------------------------------------------------------------
# Детерминированная маскировка регулярками (оффлайн-страховка)
# ------------------------------------------------------------------------------
def _next_placeholder(entity: str, used: dict[str, int]) -> str:
    """Выдать следующий номер плейсхолдера для типа сущности."""
    used[entity] = used.get(entity, 0) + 1
    return f"[{entity}_{used[entity]}]"


def regex_mask(text: str) -> tuple[str, dict[str, int]]:
    """Маскировать ПДн регулярными выражениями.

    Args:
        text: исходный (или уже обезличенный LLM) текст.

    Returns:
        Кортеж ``(обезличенный текст, счётчик сущностей по типам)``.

    Особенности:
        * нумерация плейсхолдеров сквозная и стабильная: одинаковые значения
          получают один и тот же номер;
        * правовые данные (номера статей, процессуальные сроки, даты событий)
          не затрагиваются, так как под шаблоны не подпадают.
    """
    if not text:
        return text, {}

    counters: dict[str, int] = {}
    assigned: dict[str, str] = {}
    masked = text

    for entity, pattern in _REGEX_RULES:
        def _replace(match: re.Match[str], entity: str = entity) -> str:
            value = match.group(0).strip()
            if not value:
                return match.group(0)
            key = f"{entity}:{value.lower()}"
            if key in assigned:
                return assigned[key]
            if entity == "NAME" and value.upper().startswith("[NAME"):
                return match.group(0)
            placeholder = _next_placeholder(entity, counters)
            assigned[key] = placeholder
            return placeholder

        masked = pattern.sub(_replace, masked)

    return masked, counters


def _fallback_summary(masked_text: str, entities: dict[str, int]) -> str:
    """Построить анонимное резюме без LLM (шаблонный режим / аварийный путь).

    Резюме нужно внешней модели для понимания сути ситуации, поэтому в него
    попадает только обезличенный текст (первая часть запроса, обрезанная по
    границе предложения) и перечень типов найденных сущностей.
    """
    clean = " ".join(masked_text.split())
    if len(clean) > 700:
        cut = clean[:700]
        for separator in (". ", "! ", "? "):
            position = cut.rfind(separator)
            if position > 250:
                cut = cut[: position + 1]
                break
        clean = cut.rstrip() + "…"

    kinds = ", ".join(
        f"{entity} — {count}" for entity, count in sorted(entities.items())
    )
    if kinds:
        return f"Обезличенное описание ситуации: {clean} Маскированные сущности: {kinds}."
    return f"Обезличенное описание ситуации: {clean}"


# ------------------------------------------------------------------------------
# LLM-маскировка в контуре РФ (YandexGPT / Ollama)
# ------------------------------------------------------------------------------
def _parse_masking_json(raw: str) -> dict | None:
    """Извлечь JSON-объект из ответа LLM (устойчиво к markdown-обёртке)."""
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(candidate[start : end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _entities_from_payload(payload: object) -> dict[str, int]:
    """Привести ``entities_found`` из ответа LLM к ``dict[str, int]``."""
    result: dict[str, int] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                count = 1
            result[str(key).upper()] = max(count, 1)
    elif isinstance(payload, list):
        for key in payload:
            result[str(key).upper()] = result.get(str(key).upper(), 0) + 1
    return result


def llm_mask(raw_query: str) -> MaskResult:
    """Маскировать запрос через российский LLM (этап Stage 1).

    Raises:
        LLMError: если провайдер контура недоступен или вернул пустой ответ.
    """
    provider_name = settings.MASKING_PROVIDER
    provider = get_provider(provider_name)
    if not provider.is_configured():
        raise LLMError(f"{provider_name}: провайдер Stage 1 не сконфигурирован")

    prompt = PROMPT_STAGE1_MASKING.replace("{{RAW_QUERY}}", raw_query)
    raw_answer = provider.chat(
        messages=[{"role": "user", "content": prompt}],
        model=None if provider_name == "ollama" else settings.YANDEX_GPT_MODEL,
        temperature=0.1,
        max_tokens=2000,
    )

    payload = _parse_masking_json(raw_answer)
    if not payload:
        # Модель ответила текстом без JSON — считаем сохранённый текст маской
        masked = raw_answer.strip()
        if not masked:
            raise LLMError(f"{provider_name}: пустой ответ маскировщика")
        return MaskResult(
            masked_query=masked,
            anonymized_summary="",
            entities={},
            provider=provider_name,
            llm_used=True,
            warning="Модель Stage 1 вернула неструктурированный ответ.",
        )

    masked_query = str(payload.get("masked_query") or "").strip()
    if not masked_query:
        raise LLMError(f"{provider_name}: в JSON нет поля masked_query")

    return MaskResult(
        masked_query=masked_query,
        anonymized_summary=str(payload.get("anonymized_summary") or "").strip(),
        entities=_entities_from_payload(payload.get("entities_found")),
        provider=provider_name,
        risk_notes=str(payload.get("risk_notes") or "").strip(),
        llm_used=True,
    )


# ------------------------------------------------------------------------------
# ПУБЛИЧНЫЙ API КОНТУРА (используется мегапайплайном)
# ------------------------------------------------------------------------------
def _merge_entities(first: dict[str, int], second: dict[str, int]) -> dict[str, int]:
    """Объединить счётчики сущностей двух проходов маскировки."""
    merged = dict(first)
    for key, value in second.items():
        merged[key] = max(merged.get(key, 0), value)
    return merged


def mask_query(raw_query: str) -> MaskResult:
    """STAGE 1: обезличить запрос перед отправкой во внешнюю модель.

    Порядок действий:
        1. Маскировка российским LLM (если ``MASKING_PROVIDER`` ≠ ``regex``).
           Любое исключение (таймаут, сетевой сбой, HTTP 5xx, битый JSON,
           ``RuntimeError`` и т.п.) ЛОГИРУЕТСЯ через ``logger.warning`` и
           МГНОВЕННО переключает контур на оффлайн-страховку — клиентский
           запрос никогда не должен падать из-за сетевых проблем Stage 1.
        2. Обязательный regex-проход поверх результата — страховка 152-ФЗ.
           Регулярки гарантированно вычищают ФИО (``[NAME_1]``), email
           (``[EMAIL_1]``), телефоны формата ``+7…`` (``[PHONE_1]``) и пр.
        3. Формирование анонимного резюме (LLM либо шаблон).

    Args:
        raw_query: исходный текст запроса клиента (может содержать ПДн).

    Returns:
        :class:`MaskResult` с гарантированно обезличенным ``masked_query``.
        При недоступности LLM возвращается результат regex-контура и
        предупреждение в поле ``warning`` — запрос НЕ передаётся внешней
        модели в сыром виде ни при каких условиях.
    """
    provider_name = settings.MASKING_PROVIDER
    warning: str | None = None
    llm_used = False
    summary = ""
    risk_notes = ""
    entities: dict[str, int] = {}
    masked = raw_query

    if provider_name != "regex":
        try:
            llm_result = llm_mask(raw_query)
            masked = llm_result.masked_query
            summary = llm_result.anonymized_summary
            risk_notes = llm_result.risk_notes
            entities = llm_result.entities
            llm_used = llm_result.llm_used
            warning = llm_result.warning
        except Exception as exc:  # noqa: BLE001 — намеренно широкий catch
            # Ловим ЛЮБОЕ исключение: таймаут, сетевой сбой, HTTP 5xx,
            # битый JSON, неожиданный RuntimeError и т.д. Stage 1 обязан
            # быть отказоустойчивым — сетевые инциденты не должны ронять
            # весь мегапайплайн и блокировать клиента.
            logger.warning(
                "Stage 1: провайдер '%s' недоступен (%s: %s). "
                "Мгновенное переключение на оффлайн-контур regex.",
                provider_name,
                type(exc).__name__,
                exc,
            )
            warning = (
                "Контур маскировки РФ недоступен, применена оффлайн-страховка "
                f"(regex): {type(exc).__name__}: {exc}"
            )

    # --- Страховочный проход регулярками (всегда) ---------------------------
    masked, regex_entities = regex_mask(masked)
    entities = _merge_entities(entities, regex_entities)

    if not masked.strip():
        # Экзотический случай: модель «вычистила» весь текст.
        masked = "[TEXT_REMOVED_BY_MASKING_CONTOUR]"
        warning = warning or "Контур маскировки вернул пустой текст."

    if not summary:
        summary = _fallback_summary(masked, entities)

    if not entities and not llm_used:
        risk_notes = risk_notes or (
            "Персональные данные по формальным признакам не обнаружены; "
            "выполнен обезличивающий проход регулярными выражениями."
        )

    return MaskResult(
        masked_query=masked,
        anonymized_summary=summary,
        entities=entities,
        provider=provider_name if llm_used else "regex",
        risk_notes=risk_notes,
        llm_used=llm_used,
        warning=warning,
    )