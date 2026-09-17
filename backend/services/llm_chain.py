"""Мегапайплайн обработки юридического запроса (разделы 2 и 3 ТЗ).

Схема обработки:

    STAGE 1: КОНТУР РФ (152-ФЗ)
      YandexGPT / Ollama + обязательная regex-страховка → [NAME_1], [ADDRESS_1]
        ↓ передаётся только обезличенный текст
    STAGE 2: Web-фактчекинг (2–3 источника) → Gemini 2.5 Flash (AITunnel)
        ↓
    ФИНАЛ: LLM-1 (черновик, <=150 слов) → LLM-2 (асессор, 400–700 слов)

Failover-оркестратор (раздел 3 ТЗ):
    * ``PRIMARY_PROVIDER=aitunnel`` → резерв YandexGPT;
    * ``PRIMARY_PROVIDER=yandex``   → резерв AITunnel;
    * на Stage 2 (анализ + фактчекинг) цепочка ВСЕГДА заканчивается
      на ``aitunnel`` — см. :func:`stage2_fallback_chain`. Это защищает
      Stage 2 от падения Yandex (например, UnicodeEncodeError из-за
      кириллицы в HTTP-заголовках): код автоматически дойдёт до Gemini.
    * при падении обоих провайдеров возвращается структурированная ошибка,
      и обработчик API отдаёт HTTP 502, не списывая бесплатный запрос.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from backend.config import settings
from backend.services import prompts
from backend.services.pii_masker import MaskResult, mask_query
from backend.services.providers import LLMError, fallback_chain, get_provider
from backend.services.web_factcheck import Source, gather_sources

logger = logging.getLogger("clickjurist.llm_chain")

# Сколько попыток на каждом провайдере внутри цепочки failover
ATTEMPTS_PER_PROVIDER = 2
RETRY_SLEEP_BASE_S = 5


@dataclass
class PipelineResult:
    """Итог обработки запроса клиента."""

    final: str = ""
    draft: str = ""
    reference: str = ""
    mask: MaskResult | None = None
    sources: list[dict[str, str]] = field(default_factory=list)
    citation_verified: bool = False
    anonymized: bool = True
    stage1_provider: str = "regex"
    stage2_provider: str = ""
    warning: str | None = None
    error: str | None = None
    used_failover: bool = False


def _call_with_failover(
    messages: list[dict[str, str]],
    model: str,
    temperature: float,
    max_tokens: int,
    stage: str,
    chain: list[str] | None = None,
) -> tuple[str, str, bool]:
    """Вызвать LLM с автоматическим переходом на резервный провайдер.

    Args:
        messages: сообщения чата (role/content).
        model: имя модели для основного провайдера.
        temperature: температура генерации.
        max_tokens: ограничение длины ответа.
        stage: метка этапа для журнала (``stage1`` / ``stage2``).
        chain: явная цепочка провайдеров. Если ``None`` — берётся
            :func:`fallback_chain`. Используется в Stage 2, чтобы
            гарантировать ``aitunnel`` финальным fallback'ом.

    Returns:
        Кортеж ``(текст, имя_провайдера, использован_ли_failover)``.

    Raises:
        LLMError: если ВСЕ провайдеры цепочки недоступны.
    """
    errors: list[str] = []
    provider_chain = chain if chain is not None else fallback_chain()
    # Защита от дублей в цепочке — иначе один и тот же провайдер
    # может быть вызван дважды подряд при попытке сделать failover.
    seen: set[str] = set()
    deduped_chain: list[str] = []
    for name in provider_chain:
        if name not in seen:
            seen.add(name)
            deduped_chain.append(name)
    provider_chain = deduped_chain
    for index, provider_name in enumerate(provider_chain):
        provider = get_provider(provider_name)
        if not provider.is_configured():
            errors.append(f"{provider_name}: провайдер не сконфигурирован")
            continue

        for attempt in range(ATTEMPTS_PER_PROVIDER):
            try:
                logger.info(
                    "Вызов LLM-провайдера",
                    extra={"provider": provider_name, "stage": stage},
                )
                text = provider.chat(
                    messages=messages,
                    model=model if provider_name == settings.PRIMARY_PROVIDER else None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return text, provider_name, index > 0
            except LLMError as exc:
                errors.append(f"{provider_name} (попытка {attempt + 1}): {exc}")
                logger.warning(
                    "Сбой провайдера, повтор/переключение",
                    extra={"provider": provider_name, "stage": stage},
                )
                if attempt < ATTEMPTS_PER_PROVIDER - 1:
                    time.sleep(RETRY_SLEEP_BASE_S * (attempt + 1))

    raise LLMError("Все провайдеры недоступны. " + " | ".join(errors))


# --- STAGE 2: анализ с веб-фактчекингом ---------------------------------------
def stage2_fallback_chain() -> list[str]:
    """Цепочка провайдеров для Stage 2 с гарантированным failover на aitunnel.

    В Stage 2 (правовой анализ + веб-фактчекинг) Gemini 2.5 Flash через
    AITunnel — основной источник качества, поэтому он ВСЕГДА должен быть
    последним шансом. Логика:

        * берём стандартную :func:`fallback_chain` (primary/secondary);
        * если ``aitunnel`` уже в цепочке, но не в конце — переставляем
          его в конец, чтобы он срабатывал именно как финальный fallback;
        * если ``aitunnel`` отсутствует — добавляем его в конец.

    Это решает проблему, когда ``PRIMARY_PROVIDER=aitunnel`` и при падении
    AITunnel + Yandex на Stage 2 цепочка заканчивалась ничем, либо
    ``PRIMARY_PROVIDER=yandex`` и Yandex падал с UnicodeEncodeError — теперь
    код гарантированно дойдёт до ``aitunnel`` (Gemini).
    """
    chain = list(fallback_chain())
    if "aitunnel" in chain:
        # Переставляем aitunnel в конец как финальный fallback.
        chain = [name for name in chain if name != "aitunnel"]
    chain.append("aitunnel")
    return chain


def sources_as_text(sources: list[Source], mask: MaskResult) -> str:
    """Собрать блок «Внешние источники» для промпта Stage 2."""
    if not sources:
        return prompts.SOURCES_EMPTY

    lines: list[str] = []
    for number, source in enumerate(sources, start=1):
        snippet = (source.snippet or "").strip().replace("\n", " ")[:600]
        lines.append(
            f"[{number}] {source.title}\n"
            f"    URL: {source.url}\n"
            f"    Фрагмент: {snippet or 'фрагмент недоступен'}"
        )

    if len(sources) < settings.WEB_SEARCH_MIN_SOURCES:
        lines.append("")
        lines.append(prompts.SOURCES_CONFLICT_NOTE)

    if mask.risk_notes:
        lines.append("")
        lines.append(f"Замечание контура маскировки данных: {mask.risk_notes}")

    return "\n".join(lines)


def run_stage2(mask: MaskResult, sources: list[Source]) -> tuple[str, str, bool]:
    """Выполнить Stage 2 — правовой анализ обезличенного запроса.

    Args:
        mask: результат Stage 1 (маскировка ПДн).
        sources: проверенные веб-источники (0–3 штуки).

    Returns:
        ``(текст_анализа, провайдер, использован_ли_failover)``.

    Notes:
        В Stage 2 используется **специальная** цепочка failover — см.
        :func:`stage2_fallback_chain`. Это гарантирует, что если Yandex
        упал (например, с UnicodeEncodeError из-за кириллицы в заголовках
        или по любой другой причине), система автоматически переключится
        на AITunnel (Gemini 2.5 Flash) и обработает запрос до конца.
    """
    prompt = prompts.PROMPT_STAGE2_ANALYSIS
    prompt = prompt.replace("{{MIN_SOURCES}}", str(settings.WEB_SEARCH_MIN_SOURCES))
    prompt = prompt.replace("{{MAX_SOURCES}}", str(settings.WEB_SEARCH_MAX_SOURCES))
    prompt = prompt.replace("{{SOURCES}}", sources_as_text(sources, mask))
    prompt = prompt.replace("{{MASKED_QUERY}}", mask.masked_query)
    prompt = prompt.replace("{{ANONYMIZED_SUMMARY}}", mask.anonymized_summary)

    return _call_with_failover(
        messages=[
            {"role": "system", "content": prompts.PROMPT_LLM_1},
            {"role": "user", "content": prompt},
        ],
        model=settings.MODEL_GEMINI_FACTCHECK,
        temperature=0.2,
        max_tokens=3000,
        stage="stage2",
        chain=stage2_fallback_chain(),
    )


# --- ФИНАЛ: LLM-1 (черновик) → LLM-2 (асессор) --------------------------------
def run_draft(masked_query: str, analysis: str = "") -> tuple[str, str, bool]:
    """LLM-1 — быстрый черновик юридической консультации (<=150 слов)."""
    user_content = masked_query
    if analysis:
        user_content = (
            f"{masked_query}\n\n"
            "Материалы проверки (используй как опору, не копируй целиком):\n"
            f"{analysis}"
        )
    return _call_with_failover(
        messages=[
            {"role": "system", "content": prompts.PROMPT_LLM_1},
            {"role": "user", "content": user_content},
        ],
        model=settings.MODEL_LLM_1,
        temperature=0.2,
        max_tokens=settings.MAX_TOKENS_LLM_1,
        stage="llm1",
    )


def run_reference(masked_query: str, draft: str) -> tuple[str, str, bool]:
    """LLM-2 — критическая проверка черновика и сборка эталонного ответа."""
    user_content = (
        f"ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n{masked_query}\n\n"
        f"ЧЕРНОВИК ОТ LLM-1:\n{draft}"
    )
    return _call_with_failover(
        messages=[
            {"role": "system", "content": prompts.PROMPT_LLM_2},
            {"role": "user", "content": user_content},
        ],
        model=settings.MODEL_LLM_2,
        temperature=0.3,
        max_tokens=settings.MAX_TOKENS_LLM_2,
        stage="llm2",
    )


# --- ВЕРИФИКАЦИЯ ЮРИДИЧЕСКИХ ССЫЛОК -------------------------------------------
CITATION_PATTERN = re.compile(
    r"(?:ст\.|стать[ия]\s|статьи\s|п\.\s?\d|ч\.\s?\d|пункт\s?\d)"
    r"[\s\d\.\-—,]*"
    r"(?:ГК|УК|ТК|КоАП|НК|СК|ЖК|ЗК|ГПК|УПК|КАС|АПК)?\s?РФ",
    re.IGNORECASE,
)
MIN_CITATIONS = 1


def extract_citations(text: str) -> list[str]:
    """Извлечь из ответа ссылки на нормы права РФ."""
    return [match.group(0).strip() for match in CITATION_PATTERN.finditer(text)]


def verify_citations(text: str, sources: list[Source]) -> bool:
    """Проверить наличие правовых ссылок и подтверждения источниками.

    Логика раздела 2 ТЗ:
        * хотя бы одна ссылка на норму права РФ обязательна;
        * если источников меньше минимума — ссылки не считаются
          верифицированными (это передаётся во фронтенд флагом
          ``citation_verified=False``).
    """
    citations = extract_citations(text)
    if len(citations) < MIN_CITATIONS:
        return False
    has_min_sources = len(sources) >= settings.WEB_SEARCH_MIN_SOURCES
    return bool(has_min_sources)


def normalize_final(text: str) -> str:
    """Убрать служебные заголовки асессора и лишние пустые строки."""
    cleaned = text.strip()
    for marker in ("## Итоговый ответ", "## Итоговый Ответ", "Итоговый ответ:"):
        if cleaned.upper().startswith(marker.upper()):
            cleaned = cleaned[len(marker):].lstrip(" :\n")
            break
    else:
        index = cleaned.upper().find("## ИТОГОВЫЙ ОТВЕТ")
        if index != -1:
            cleaned = cleaned[index + len("## Итоговый ответ"):].lstrip(" :\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def attach_disclaimer(text: str, *, for_document: bool = False) -> str:
    """Добавить обязательный дисклеймер о применении генеративного ИИ.

    Args:
        text: исходный текст ответа.
        for_document: если ``True`` — дисклеймер НЕ добавляется, потому что
            текст идёт в официальный документ (иск / претензия / жалоба),
            который подаётся в суд или гос. орган. Дисклеймер остаётся
            только для консультаций и чек-листов, отображаемых в UI.
    """
    if not text:
        return text
    if for_document:
        # Официальные документы не должны содержать служебных пометок сервиса.
        return text
    if prompts.AI_DISCLAIMER in text:
        return text
    return f"{text.strip()}\n\n---\n{prompts.AI_DISCLAIMER}"


# --- ДОПОЛНИТЕЛЬНЫЕ СЕРВИСЫ ---------------------------------------------------
def draft_checklist(masked_query: str, final_answer: str) -> str:
    """Сгенерировать чек-лист действий на основе готовой консультации.

    Чек-лист содержит жёсткие локализованные данные (конкретный суд,
    срок давности, досудебный порядок) и требует расширенного лимита
    ``max_tokens`` (см. ``settings.MAX_TOKENS_CHECKLIST``), чтобы модель
    не обрывала структуру из 4 шагов с подробными выгодами.
    """
    system_prompt = (
        prompts.PROMPT_CHECKLIST
        .replace("{{MASKED_QUERY}}", masked_query)
        .replace("{{FINAL_ANSWER}}", final_answer)
    )
    text, _, _ = _call_with_failover(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Сформируй чек-лист по структуре системного промпта."},
        ],
        model=settings.MODEL_LLM_1,
        temperature=0.2,
        max_tokens=getattr(settings, "MAX_TOKENS_CHECKLIST", settings.MAX_TOKENS_LLM_1),
        stage="checklist",
    )
    return text


DOC_TYPE_TITLES: dict[str, str] = {
    "complaint": "Жалоба в вышестоящий орган или контролирующую инстанцию",
    "claim": "Досудебная претензия продавцу/исполнителю",
    "lawsuit": "Исковое заявление в суд",
    "court_order_cancellation": "Заявление об отмене судебного приказа",
}


def _document_prompt(doc_type: str) -> str:
    """Подобрать специализированный системный промпт по типу документа.

    Логика маршрутизации (см. prompts.py):
        • ``lawsuit``                 → :data:`PROMPT_LAWSUIT`
        • ``claim``                   → :data:`PROMPT_CLAIM`
        • ``complaint``               → :data:`PROMPT_COMPLAINT`
        • ``court_order_cancellation``→ :data:`PROMPT_COURT_ORDER_CANCELLATION`

    Для неизвестных значений возвращается :data:`PROMPT_LAWSUIT` как fallback.
    """
    return {
        "lawsuit": prompts.PROMPT_LAWSUIT,
        "claim": prompts.PROMPT_CLAIM,
        "complaint": prompts.PROMPT_COMPLAINT,
        "court_order_cancellation": prompts.PROMPT_COURT_ORDER_CANCELLATION,
    }.get(doc_type, prompts.PROMPT_LAWSUIT)


def draft_document(masked_query: str, doc_type: str = "lawsuit") -> str:
    """Сгенерировать шаблон процессуального документа.

    Для искового заявления / претензии / жалобы / отмены судебного приказа
    используется СПЕЦИАЛИЗИРОВАННЫЙ системный промпт с развёрнутой
    правовой аргументацией и подробной шапкой по АПК/ГПК РФ. Лимит
    ``max_tokens`` увеличен (см. ``settings.MAX_TOKENS_DOCUMENT``), чтобы
    модель не обрывала генерацию на середине сложного документа.
    """
    system_prompt = _document_prompt(doc_type)
    text, _, _ = _call_with_failover(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Ситуация (обезличено):\n{masked_query}"},
        ],
        model=settings.MODEL_LLM_1,
        temperature=0.2,
        max_tokens=getattr(settings, "MAX_TOKENS_DOCUMENT", settings.MAX_TOKENS_LLM_1),
        stage="document",
    )
    return text


# --- ГЛАВНЫЙ ПАЙПЛАЙН ---------------------------------------------------------
def run_pipeline(raw_query: str, with_stage2: bool = True) -> PipelineResult:
    """Полный мегапайплайн обработки запроса клиента.

    Шаги:
        1. Stage 1 — маскировка персональных данных в контуре РФ.
        2. Stage 2 — сбор 2–3 веб-источников и правовой анализ Gemini 2.5 Flash.
        3. Финал — LLM-1 (черновик) → LLM-2 (эталонный ответ асессора).

    Args:
        raw_query: сырой текст запроса клиента (может содержать ПДн).
        with_stage2: выполнять ли веб-фактчекинг и анализ (обычно ``True``).

    Returns:
        :class:`PipelineResult` с эталонным ответом либо описанием ошибки.
        Ошибки НЕ поднимаются наружу — API возвращает их как HTTP 502.
    """
    started = time.time()
    result = PipelineResult()

    # --- Шаг 1: контур маскировки ПДн (российский LLM, раздел 2 ТЗ) ----------
    try:
        mask = mask_query(raw_query)
    except Exception as exc:  # страховка: маскировка не должна ломать запрос
        logger.error("Критическая ошибка контура маскировки")
        result.error = f"Ошибка контура маскировки персональных данных: {exc}"
        return result

    result.mask = mask
    result.anonymized = True
    result.stage1_provider = mask.provider
    result.warning = mask.warning

    # --- Шаг 2: веб-фактчекинг и правовой анализ ---------------------------
    sources: list[Source] = []
    analysis = ""
    if with_stage2:
        try:
            sources = gather_sources(mask.anonymized_summary or mask.masked_query)
        except Exception:
            logger.warning("Веб-фактчекинг недоступен, продолжаем без источников")
            sources = []

        try:
            analysis, provider, used_failover = run_stage2(mask, sources)
            result.stage2_provider = provider
            result.used_failover = used_failover
        except LLMError as exc:
            logger.error("Stage 2 недоступен: анализ на внешней модели невозможен")
            result.error = str(exc)
            return result

    # --- Шаг 3: черновик LLM-1 → эталон LLM-2 ------------------------------
    try:
        draft, _, failover_1 = run_draft(mask.masked_query, analysis)
        result.draft = draft
    except LLMError as exc:
        result.error = str(exc)
        return result

    try:
        reference, provider_2, failover_2 = run_reference(mask.masked_query, draft)
        result.reference = reference
        result.final = normalize_final(reference)
        result.stage2_provider = provider_2 or result.stage2_provider
        result.used_failover = result.used_failover or failover_1 or failover_2
    except LLMError:
        # Асессор недоступен — отдаём черновик, это лучше, чем ошибка
        logger.warning("Асессор недоступен, возвращаем черновик LLM-1")
        result.final = normalize_final(draft)
        result.warning = (
            "Эталонный ответ асессора недоступен: используется проверенный черновик "
            "LLM-1."
        )

    if not result.final.strip():
        result.error = "Модели вернули пустой ответ. Переформулируйте запрос."
        return result

    result.sources = [{"title": s.title, "url": s.url} for s in sources]
    result.citation_verified = verify_citations(result.final, sources)

    if not result.citation_verified and not result.warning:
        result.warning = (
            "Ссылки на нормы права не подтверждены внешними источниками: "
            "данные требуют дополнительной проверки по официальной редакции."
        )

    logger.info(
        "Пайплайн завершён",
        extra={
            "stage": "pipeline",
            "provider": result.stage2_provider,
            "latency_ms": int((time.time() - started) * 1000),
        },
    )
    return result


def run_consultation(raw_query: str) -> PipelineResult:
    """Удобная обёртка: обработка запроса с полным набором этапов."""
    return run_pipeline(raw_query, with_stage2=True)


# ==============================================================================
# СТРИМИНГОВЫЙ ПАЙПЛАЙН (Шаг 1 ТЗ prompt170926.md)
# ==============================================================================
def stream_reference(
    masked_query: str,
    analysis: str = "",
    on_token=None,
) -> tuple[str, str, bool]:
    """Стриминг эталонного ответа LLM-2 (асессор) токен за токеном.

    Использует :meth:`BaseProvider.stream_chat` если провайдер его поддерживает
    (например, AITunnel). Если стриминг недоступен — автоматически откатывается
    на обычный :func:`run_reference` и эмитит результат одним «токеном».

    Args:
        masked_query: обезличенный запрос клиента.
        analysis: результат Stage 2 (правовой анализ + веб-фактчекинг).
        on_token: callback ``callable(str)`` — вызывается на каждый фрагмент текста.

    Returns:
        ``(полный_текст, имя_провайдера, использован_ли_failover)``.
    """
    user_content = (
        f"ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n{masked_query}\n\n"
        f"ЧЕРНОВИК ОТ LLM-1:\n{analysis}"
    )
    messages = [
        {"role": "system", "content": prompts.PROMPT_LLM_2},
        {"role": "user", "content": user_content},
    ]

    chain = fallback_chain()
    seen: set[str] = set()
    deduped_chain: list[str] = []
    for name in chain:
        if name not in seen:
            seen.add(name)
            deduped_chain.append(name)
    chain = deduped_chain

    errors: list[str] = []
    for index, provider_name in enumerate(chain):
        provider = get_provider(provider_name)
        if not provider.is_configured():
            errors.append(f"{provider_name}: не сконфигурирован")
            continue
        try:
            accumulated: list[str] = []
            for piece in provider.stream_chat(
                messages=messages,
                model=settings.MODEL_LLM_2 if provider_name == settings.PRIMARY_PROVIDER else None,
                temperature=0.3,
                max_tokens=settings.MAX_TOKENS_LLM_2,
            ):
                accumulated.append(piece)
                if on_token is not None:
                    try:
                        on_token(piece)
                    except Exception:  # noqa: BLE001
                        pass
            full_text = "".join(accumulated).strip()
            if full_text:
                return full_text, provider_name, index > 0
            errors.append(f"{provider_name}: пустой стрим")
        except LLMError as exc:
            errors.append(f"{provider_name}: {exc}")
            logger.warning(
                "Стриминг недоступен, пробуем следующего провайдера",
                extra={"provider": provider_name, "stage": "stream_reference"},
            )
            continue

    raise LLMError("Стриминг LLM-2 недоступен: " + " | ".join(errors))


def run_pipeline_streaming(
    raw_query: str,
    on_event=None,
) -> PipelineResult:
    """Полный мегапайплайн со стримингом финального ответа (Шаг 1 ТЗ prompt170926.md).

    Эмитит события в callback ``on_event(event_dict)``:
        * ``{"type": "started", "stage": "masking"}``
        * ``{"type": "progress", "stage": "...", "progress": N}``
        * ``{"type": "token", "text": "..."}`` — каждый фрагмент текста
        * ``{"type": "sources", "sources": [...]}``
        * ``{"type": "meta", "stage1_provider": "...", "stage2_provider": "..."}``
        * ``{"type": "completed", "result": {...}}``
        * ``{"type": "failed", "error": "..."}``

    Returns:
        :class:`PipelineResult` с финальным текстом.
    """
    started = time.time()
    result = PipelineResult()

    def emit(event: dict) -> None:
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001
                pass

    emit({"type": "started", "stage": "masking"})

    # --- Шаг 1: маскировка ПДн ---
    try:
        mask = mask_query(raw_query)
    except Exception as exc:  # noqa: BLE001
        result.error = f"Ошибка контура маскировки: {exc}"
        emit({"type": "failed", "error": result.error})
        return result

    result.mask = mask
    result.anonymized = True
    result.stage1_provider = mask.provider
    result.warning = mask.warning
    emit({"type": "progress", "stage": "masking_done", "progress": 20})

    # --- Шаг 2: веб-фактчекинг ---
    sources: list[Source] = []
    try:
        sources = gather_sources(mask.anonymized_summary or mask.masked_query)
    except Exception:  # noqa: BLE001
        logger.warning("Веб-фактчекинг недоступен, продолжаем без источников")
        sources = []
    emit({"type": "progress", "stage": "web_search_done", "progress": 35})
    emit({"type": "sources", "sources": [{"title": s.title, "url": s.url} for s in sources]})

    # --- Шаг 3: черновик LLM-1 (не стримим — это короткий черновик) ---
    try:
        draft, _, _ = run_draft(mask.masked_query, "")
        result.draft = draft
    except LLMError as exc:
        result.error = f"LLM-1 (черновик) недоступен: {exc}"
        emit({"type": "failed", "error": result.error})
        return result
    emit({"type": "progress", "stage": "draft_done", "progress": 50})

    # --- Шаг 4: стриминг эталонного ответа LLM-2 ---
    accumulated_tokens: list[str] = []

    def _on_token(piece: str) -> None:
        accumulated_tokens.append(piece)
        emit({"type": "token", "text": piece})

    try:
        reference, provider_2, _ = stream_reference(
            mask.masked_query, draft, on_token=_on_token
        )
        result.reference = reference
        result.final = normalize_final(reference)
        result.stage2_provider = provider_2
    except LLMError as exc:
        logger.warning("Стриминг LLM-2 упал, возвращаем черновик: %s", exc)
        result.final = normalize_final(draft)
        result.warning = (
            "Эталонный ответ асессора недоступен: используется проверенный черновик LLM-1."
        )

    if not result.final.strip():
        result.error = "Модели вернули пустой ответ. Переформулируйте запрос."
        emit({"type": "failed", "error": result.error})
        return result

    result.sources = [{"title": s.title, "url": s.url} for s in sources]
    result.citation_verified = verify_citations(result.final, sources)

    if not result.citation_verified and not result.warning:
        result.warning = (
            "Ссылки на нормы права не подтверждены внешними источниками: "
            "данные требуют дополнительной проверки по официальной редакции."
        )

    emit({
        "type": "meta",
        "stage1_provider": result.stage1_provider,
        "stage2_provider": result.stage2_provider,
        "warning": result.warning or "",
    })
    emit({"type": "progress", "stage": "done", "progress": 100})

    logger.info(
        "Стриминговый пайплайн завершён",
        extra={
            "stage": "pipeline_streaming",
            "provider": result.stage2_provider,
            "latency_ms": int((time.time() - started) * 1000),
        },
    )
    return result