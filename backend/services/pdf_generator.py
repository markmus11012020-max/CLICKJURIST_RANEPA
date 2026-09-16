"""Генерация PDF-отчётов ClickJurist Production.

Ключевая задача — корректная кириллица. Шрифт подбирается в таком порядке:
    1. ``PDF_FONT_PATH`` из настроек (явный путь имеет приоритет);
    2. DejaVuSans (``DejaVuSans.ttf`` / ``DejaVuSans-Bold.ttf``);
    3. Arial / Liberation Sans — системные шрифты Windows и Linux.

Если ни один шрифт не найден, документ всё равно формируется: используется
встроенный Helvetica (латиница), а в текст добавляется предупреждение —
это лучше, чем упасть с 500-й ошибкой.
"""
from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

from backend.config import PROJECT_ROOT, settings
from backend.services import prompts

logger = logging.getLogger("clickjurist.pdf")

# Кандидаты на шрифты с поддержкой кириллицы (обычный и жирный начерк)
_REGULAR_CANDIDATES: tuple[str, ...] = (
    "DejaVuSans.ttf",
    "arial.ttf",
    "Arial.ttf",
    "LiberationSans-Regular.ttf",
    "NotoSans-Regular.ttf",
)
_BOLD_CANDIDATES: tuple[str, ...] = (
    "DejaVuSans-Bold.ttf",
    "arialbd.ttf",
    "Arial Bold.ttf",
    "LiberationSans-Bold.ttf",
    "NotoSans-Bold.ttf",
)

_WINDOWS_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_LINUX_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts",
)


@dataclass
class FontSet:
    """Подобранная пара шрифтов (обычный и полужирный)."""

    name: str
    regular_path: str | None = None
    bold_path: str | None = None
    cyrillic: bool = False


def _iter_font_dirs() -> list[str]:
    """Каталоги поиска шрифтов для текущей платформы."""
    dirs = [str(PROJECT_ROOT / "assets" / "fonts"), _WINDOWS_FONT_DIR, *_LINUX_FONT_DIRS]
    return [path for path in dirs if os.path.isdir(path)]


def _find_font(candidates: tuple[str, ...]) -> str | None:
    """Найти первый существующий файл шрифта из списка кандидатов."""
    for directory in _iter_font_dirs():
        for filename in candidates:
            path = os.path.join(directory, filename)
            if os.path.isfile(path):
                return path
    return None


def resolve_fonts() -> FontSet:
    """Подобрать шрифты с поддержкой кириллицы.

    Returns:
        :class:`FontSet`; при ``cyrillic=False`` используется встроенный
        Helvetica и в документе появляется предупреждение.
    """
    explicit = (settings.PDF_FONT_PATH or "").strip()
    if explicit and os.path.isfile(explicit):
        return FontSet(
            name="custom",
            regular_path=explicit,
            bold_path=_find_font(_BOLD_CANDIDATES),
            cyrillic=True,
        )

    regular = _find_font(_REGULAR_CANDIDATES)
    if regular and os.path.basename(regular).lower().startswith("dejavu"):
        return FontSet(
            name="DejaVuSans",
            regular_path=regular,
            bold_path=_find_font(_BOLD_CANDIDATES),
            cyrillic=True,
        )
    if regular:
        return FontSet(
            name="system-cyrillic",
            regular_path=regular,
            bold_path=_find_font(_BOLD_CANDIDATES),
            cyrillic=True,
        )
    return FontSet(name="Helvetica", cyrillic=False)


# --- Рендеринг документа ------------------------------------------------------
def _register_styles(fonts: FontSet) -> dict[str, object]:
    """Зарегистрировать шрифты в ReportLab и вернуть словарь стилей."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular_name = "Helvetica"
    bold_name = "Helvetica-Bold"

    if fonts.cyrillic and fonts.regular_path:
        pdfmetrics.registerFont(TTFont("CJ-Regular", fonts.regular_path))
        regular_name = "CJ-Regular"
        if fonts.bold_path:
            pdfmetrics.registerFont(TTFont("CJ-Bold", fonts.bold_path))
            bold_name = "CJ-Bold"
        else:
            bold_name = regular_name

    return {
        "title": ParagraphStyle(
            "CjTitle",
            fontName=bold_name,
            fontSize=16,
            leading=20,
            spaceAfter=8 * mm,
            alignment=1,
        ),
        "heading": ParagraphStyle(
            "CjHeading",
            fontName=bold_name,
            fontSize=12,
            leading=16,
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
        ),
        "body": ParagraphStyle(
            "CjBody",
            fontName=regular_name,
            fontSize=10.5,
            leading=15,
            spaceAfter=2 * mm,
        ),
        "small": ParagraphStyle(
            "CjSmall",
            fontName=regular_name,
            fontSize=8,
            leading=11,
            textColor="#555555",
            spaceBefore=4 * mm,
        ),
    }


def _escape_markup(text: str) -> str:
    """Экранировать служебные символы ReportLab (``<``, ``>``, ``&``)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _markdown_to_flowables(markup: str, styles: dict[str, object]) -> list:
    """Преобразовать простой markdown (заголовки/абзацы) в поток ReportLab."""
    from reportlab.platypus import Paragraph, Spacer

    flowables: list = []
    for raw_line in markup.split("\n"):
        line = raw_line.strip()
        if not line:
            flowables.append(Spacer(1, 2 * 2.83465))
            continue
        if line.startswith("### "):
            flowables.append(
                Paragraph(_escape_markup(line[4:]), styles["heading"])
            )
        elif line.startswith("## "):
            flowables.append(Paragraph(_escape_markup(line[3:]), styles["heading"]))
        elif line.startswith("# "):
            flowables.append(Paragraph(_escape_markup(line[2:]), styles["heading"]))
        elif line.startswith(("- ", "— ", "* ")):
            flowables.append(
                Paragraph(
                    "&bull;&nbsp;" + _escape_markup(line[2:]),
                    styles["body"],
                )
            )
        else:
            flowables.append(Paragraph(_escape_markup(line), styles["body"]))
    return flowables


def build_pdf(
    title: str,
    content_markup: str,
    subtitle: str = "",
    include_disclaimer: bool = True,
) -> bytes:
    """Сформировать PDF-файл и вернуть его содержимое в виде байтов.

    Args:
        title: заголовок документа (например, «Юридическая консультация»).
        content_markup: основной текст (простой markdown).
        subtitle: подзаголовок (например, дата и идентификатор сессии).
        include_disclaimer: добавлять ли дисклеймер об ошибках ИИ.

    Returns:
        Байты готового PDF-документа.

    Raises:
        RuntimeError: если сборка документа упала с невосстановимой ошибкой
            (например, файл шрифта недоступен или повреждён).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    started = time.perf_counter()
    logger.info("[PDF] Старт сборки документа (title=%r)", title)
    try:
        fonts = resolve_fonts()
        # Строгая проверка: если заявлен кириллический шрифт, а файла по
        # указанному пути нет — раньше reportlab уходил в бесконечный цикл.
        if fonts.cyrillic:
            if not fonts.regular_path or not os.path.isfile(fonts.regular_path):
                raise RuntimeError(
                    f"Файл кириллического шрифта не найден: "
                    f"regular_path={fonts.regular_path!r}. "
                    f"Проверьте PDF_FONT_PATH в .env."
                )
            if fonts.bold_path and not os.path.isfile(fonts.bold_path):
                logger.warning(
                    "[PDF] Bold-шрифт недоступен (%s), используем regular",
                    fonts.bold_path,
                )
                fonts.bold_path = None
        logger.info(
            "[PDF] Шрифты успешно зарегистрированы (name=%s, cyrillic=%s)",
            fonts.name, fonts.cyrillic,
        )
        styles = _register_styles(fonts)

        buffer = io.BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=20 * mm,
            rightMargin=15 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=title,
            author="ClickJurist",
        )

        flowables: list = [Paragraph(_escape_markup(title), styles["title"])]
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        meta = subtitle or f"Сформировано сервисом ClickJurist — {stamp}"
        flowables.append(Paragraph(_escape_markup(meta), styles["small"]))
        flowables.append(Spacer(1, 4 * mm))
        flowables.extend(_markdown_to_flowables(content_markup, styles))
        logger.info(
            "[PDF] Текст документа подготовлен для Canvas (%d flowables)",
            len(flowables),
        )

        if not fonts.cyrillic:
            flowables.append(
                Paragraph(
                    "Внимание: кириллический шрифт не найден, часть символов "
                    "может отображаться некорректно. Укажите PDF_FONT_PATH "
                    "в .env.",
                    styles["small"],
                )
            )

        if include_disclaimer:
            flowables.append(
                Paragraph(_escape_markup(prompts.AI_DISCLAIMER), styles["small"])
            )

        document.build(flowables)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "[PDF] doc.build() успешно завершен. Файл готов. "
            "(%d мс, %d байт)",
            elapsed_ms, buffer.tell(),
        )
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.error(
            "[PDF] СБОЙ СБОРКИ через %d мс: %s: %s",
            elapsed_ms, type(exc).__name__, exc,
        )
        # Поднимаем дальше — пусть эндпоинт вернёт 500 за миллисекунды,
        # а не держит соединение открытым бесконечно.
        raise


def filename_for(service: str) -> str:
    """Сформировать безопасное имя PDF-файла для скачивания."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    safe_service = "".join(ch for ch in service if ch.isalnum() or ch in "-_") or "doc"
    return f"clickjurist-{safe_service}-{stamp}.pdf"