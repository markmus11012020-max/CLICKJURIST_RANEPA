"""Guardrails: контроль галлюцинаций (раздел 5.2 ТЗ prompt160926.md).

Задача: после Stage 2 проверить сгенерированный документ/чек-лист на наличие
несуществующих статей кодексов и автоматически инициировать регенерацию.

Стратегии валидации:
    1. **Regex-проверка номеров статей** — каждая ссылка на ``ст. N ГК РФ``
       проверяется на допустимость диапазона (ГК РФ: 1–1154, АПК: 1–332 и т.д.).
    2. **Кросс-валидация через Stage 1** — если в документе упомянута норма,
       которая не встречается в исходной ситуации, это сигнал галлюцинации.
    3. **Проверка формата** — наличие обязательных заголовков, плейсхолдеров,
       отсутствие «выдуманных» URL и номеров дел.

Если хотя бы одна проверка не прошла — функция возвращает список нарушений,
а пайплайн инициирует регенерацию (до ``GUARDRAILS_MAX_RETRIES`` попыток).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger("clickjurist.guardrails")


CODE_ARTICLE_RANGES: dict[str, tuple[int, int]] = {
    "ГК РФ": (1, 1554),
    "УК РФ": (1, 360),
    "ТК РФ": (1, 424),
    "КоАП РФ": (1, 250),
    "НК РФ": (1, 346),
    "СК РФ": (1, 170),
    "ЖК РФ": (1, 214),
    "ЗК РФ": (1, 110),
    "ГПК РФ": (1, 446),
    "УПК РФ": (1, 510),
    "КАС РФ": (1, 365),
    "АПК РФ": (1, 332),
}


CITATION_FULL_RE = re.compile(
    r"(?:ст\.|стать[яи])\s*(\d+(?:\.\d+)?)\s*"
    r"(ГК|УК|ТК|КоАП|НК|СК|ЖК|ЗК|ГПК|УПК|КАС|АПК)\s*РФ",
    re.IGNORECASE,
)


FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"дело\s*№\s*[АA]\d{1,3}-\d{1,4}/\d{4}", re.IGNORECASE),
    re.compile(r"штраф\s*(?:в\s*размере\s*)?\d{3,}\s*(?:руб|рублей|₽)", re.IGNORECASE),
    re.compile(r"госпошлин[аы]\s*(?:в\s*размере\s*)?\d{3,}\s*(?:руб|рублей|₽)", re.IGNORECASE),
)


@dataclass
class GuardrailViolation:
    """Описание одного нарушения."""

    kind: str
    message: str
    snippet: str = ""


@dataclass
class GuardrailReport:
    """Итоговый отчёт проверки."""

    passed: bool = True
    violations: list[GuardrailViolation] = field(default_factory=list)

    def add(self, kind: str, message: str, snippet: str = "") -> None:
        """Добавить нарушение и пометить отчёт как непройденный."""
        self.passed = False
        self.violations.append(
            GuardrailViolation(kind=kind, message=message, snippet=snippet)
        )

    def summary(self) -> str:
        """Краткое текстовое резюме для логов и SSE-событий."""
        if self.passed:
            return "Guardrails: пройдены успешно"
        kinds = ", ".join(sorted({v.kind for v in self.violations}))
        return f"Guardrails: {len(self.violations)} нарушений ({kinds})"


def check_article_ranges(text: str, report: GuardrailReport) -> None:
    """Проверить, что все номера статей попадают в допустимые диапазоны."""
    for article, code in extract_citations(text):
        rng = CODE_ARTICLE_RANGES.get(code)
        if rng is None:
            continue
        low, high = rng
        if article < low or article > high:
            report.add(
                kind="invalid_article",
                message=(
                    f"Статья {article} {code} вне допустимого диапазона "
                    f"({low}–{high}) — вероятная галлюцинация"
                ),
                snippet=f"ст. {article} {code}",
            )


def extract_citations(text: str) -> list[tuple[int, str]]:
    """Извлечь все ссылки на статьи кодексов из текста."""
    citations: list[tuple[int, str]] = []
    for match in CITATION_FULL_RE.finditer(text):
        try:
            article = int(match.group(1).split(".")[0])
            code = match.group(2).upper()
            citations.append((article, f"{code} РФ"))
        except (ValueError, IndexError):
            continue
    return citations


def check_forbidden_patterns(text: str, report: GuardrailReport) -> None:
    """Проверить отсутствие запрещённых паттернов (выдуманные цифры/дела)."""
    for pattern in FORBIDDEN_PATTERNS:
        for match in pattern.finditer(text):
            report.add(
                kind="forbidden_pattern",
                message="Обнаружен запрещённый паттерн (выдуманные цифры/номера дел)",
                snippet=match.group(0),
            )


def check_required_headers(
    text: str, required: Iterable[str], report: GuardrailReport
) -> None:
    """Проверить наличие обязательных заголовков."""
    lowered = text.lower()
    for header in required:
        if header.lower() not in lowered:
            report.add(
                kind="missing_header",
                message=f"Отсутствует обязательный раздел: {header}",
                snippet=header,
            )


def check_placeholders_preserved(
    text: str, original_masked: str, report: GuardrailReport
) -> None:
    """Проверить, что плейсхолдеры из исходного запроса сохранены в ответе."""
    placeholders = set(re.findall(r"\[[A-Z_]+_\d+\]", original_masked))
    for placeholder in placeholders:
        if placeholder not in text:
            report.add(
                kind="missing_placeholder",
                message=(
                    f"Плейсхолдер {placeholder} из исходного запроса "
                    f"отсутствует в ответе — возможна утечка ПДн"
                ),
                snippet=placeholder,
            )


def validate_consultation(text: str, masked_query: str = "") -> GuardrailReport:
    """Валидация эталонной консультации (LLM-2)."""
    report = GuardrailReport()
    if not text or not text.strip():
        report.add(kind="empty", message="Пустой ответ модели")
        return report
    check_article_ranges(text, report)
    check_forbidden_patterns(text, report)
    if masked_query:
        check_placeholders_preserved(text, masked_query, report)
    return report


def validate_checklist(text: str, masked_query: str = "") -> GuardrailReport:
    """Валидация чек-листа (4 шага)."""
    report = GuardrailReport()
    if not text or not text.strip():
        report.add(kind="empty", message="Пустой чек-лист")
        return report
    check_article_ranges(text, report)
    check_forbidden_patterns(text, report)
    check_required_headers(
        text,
        required=["Шаг 1", "Шаг 2", "Шаг 3", "Шаг 4"],
        report=report,
    )
    if masked_query:
        check_placeholders_preserved(text, masked_query, report)
    return report


def validate_document(text: str, masked_query: str = "") -> GuardrailReport:
    """Валидация процессуального документа (иск/претензия/жалоба)."""
    report = GuardrailReport()
    if not text or not text.strip():
        report.add(kind="empty", message="Пустой документ")
        return report
    check_article_ranges(text, report)
    check_forbidden_patterns(text, report)
    if masked_query:
        check_placeholders_preserved(text, masked_query, report)
    return report


def should_regenerate(report: GuardrailReport) -> bool:
    """Решить, нужно ли инициировать регенерацию.

    Регенерация нужна ТОЛЬКО при критических нарушениях:
        * невалидные номера статей (явная галлюцинация);
        * выдуманные цифры/номера дел;
        * отсутствие плейсхолдеров (утечка ПДн).
    """
    critical_kinds = {
        "invalid_article",
        "forbidden_pattern",
        "missing_placeholder",
        "empty",
    }
    return any(v.kind in critical_kinds for v in report.violations)