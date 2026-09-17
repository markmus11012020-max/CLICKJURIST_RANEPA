"""Генерация чистых PDF-документов для подачи в суды и гос. органы.

Ключевая задача — корректная кириллица. Шрифт подбирается в таком порядке:
    1. ``PDF_FONT_PATH`` из настроек (явный путь имеет приоритет);
    2. DejaVuSans (``DejaVuSans.ttf`` / ``DejaVuSans-Bold.ttf``);
    3. Arial / Liberation Sans — системные шрифты Windows и Linux.

Если ни один шрифт не найден, документ всё равно формируется: используется
встроенный Helvetica (латиница), а в текст добавляется предупреждение —
это лучше, чем упасть с 500-й ошибкой.

Документ формируется как чистый бланк без брендинга, заголовков сервиса,
идентификаторов сессий и дисклеймеров ИИ. В правом верхнем углу размещается
«шапка» с пустыми полями (ФИО, адрес, телефон), которые пользователь
заполняет от руки перед подачей.

Перед рендерингом текст документа проходит через ``_sanitize_content``,
которая удаляет любые следы брендинга сервиса (включая латинское
написание «ClickJurist»), дисклеймеры ИИ, заголовки/футеры и заменяет
плейсхолдеры персональных данных (``[ORG_1]``, ``[SUM_1]`` и т. п.)
на пустые линии для ручного заполнения.
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime

from reportlab.lib.colors import black

from backend.config import PROJECT_ROOT, settings

logger = logging.getLogger("кликюрист.pdf")

# Длинная пустая линия для ручного заполнения (≈ 27 символов подчёркивания).
_BLANK_LINE = "_" * 27

# Регулярные выражения для очистки текста документа.
# Латинское написание бренда в любом регистре и с любыми разделителями —
# конвертируется в кириллическое «КликЮрист».
_RE_BRAND_LATIN = re.compile(
    r"\bclick[\s\-_]?jurist\b",
    re.IGNORECASE,
)
# Каноническое кириллическое написание бренда (используется при замене).
_CYRILLIC_BRAND = "КликЮрист"
# Заголовок сервиса.
_RE_HEADER = re.compile(
    r"^\s*Юридическая\s+консультация\s+ClickJurist\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Футер с идентификатором сессии.
_RE_FOOTER = re.compile(
    r"Документ\s+сформирован\s+ClickJurist\s*[•·\-\u2022]?\s*"
    r"(?:сессия|session)\s*[:#]?\s*[A-Za-z0-9]+",
    re.IGNORECASE,
)
# Дисклеймер ИИ (две строки).
_RE_DISCLAIMER = re.compile(
    r"Генеративный\s+ИИ\s+может\s+ошибаться.*?"
    r"(?:оптимизации\s+рутинных\s+операций|оптимизации\s+рутины)\.?",
    re.IGNORECASE | re.DOTALL,
)
# Горизонтальный разделитель markdown, который обычно предшествует дисклеймеру.
_RE_HR = re.compile(r"^\s*---\s*$", re.MULTILINE)
# Плейсхолдеры вида [ORG_1], [SUM_1], [NAME_1] и т. п.
_RE_PLACEHOLDER = re.compile(
    r"\[(?:ORG|SUM|NAME|ADDRESS|PHONE|EMAIL|PASSPORT|INN|BANK|DATE|CASE)_(\d+)\]"
)
# Конструкция «ООО [ORG_1]» / «ООО «[ORG_1]»» и т. п.
_RE_OOO_PLACEHOLDER = re.compile(
    r"ООО\s+[\"«]?\[ORG_\d+\][\"»]?",
    re.IGNORECASE,
)
# Любые одиночные плейсхолдеры в кавычках/ёлочках.
_RE_QUOTED_PLACEHOLDER = re.compile(
    r"[\"«]\[(?:ORG|SUM|NAME|ADDRESS|PHONE|EMAIL|PASSPORT|INN|BANK|DATE|CASE)_\d+\][\"»]"
)

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
            fontSize=11,
            leading=15,
            spaceAfter=2 * mm,
        ),
        "shapka_label": ParagraphStyle(
            "CjShapkaLabel",
            fontName=regular_name,
            fontSize=9,
            leading=12,
            textColor=black,
        ),
        "shapka_line": ParagraphStyle(
            "CjShapkaLine",
            fontName=regular_name,
            fontSize=11,
            leading=18,
            textColor=black,
        ),
    }


def _escape_markup(text: str) -> str:
    """Экранировать служебные символы ReportLab (``<``, ``>``, ``&``)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _sanitize_content(markup: str) -> str:
    """Очистить текст документа от брендинга, дисклеймеров и плейсхолдеров.

    Удаляет:
        - латинское и кириллическое написание бренда сервиса;
        - заголовок «Юридическая консультация ClickJurist»;
        - футер «Документ сформирован ClickJurist • сессия …»;
        - двухстрочный дисклеймер «Генеративный ИИ может ошибаться…»;
        - горизонтальные разделители ``---``, которые обычно предшествуют
          дисклеймеру.

    Заменяет:
        - конструкции «ООО [ORG_1]», «ООО «[ORG_1]»» и т. п. — на пустую
          линию ``___________________________``;
        - любые одиночные плейсхолдеры ``[ORG_1]``, ``[SUM_1]`` и т. п.
          (в кавычках и без) — на ту же пустую линию.

    Args:
        markup: исходный текст документа (простой markdown).

    Returns:
        Очищенный текст, готовый для рендеринга в PDF.
    """
    if not markup:
        return ""

    text = markup

    # 1. Удаляем дисклеймер ИИ целиком (вместе с предшествующим разделителем).
    text = _RE_DISCLAIMER.sub("", text)
    text = _RE_HR.sub("", text)

    # 2. Удаляем заголовок и футер сервиса.
    text = _RE_HEADER.sub("", text)
    text = _RE_FOOTER.sub("", text)

    # 3. Конвертируем латинское написание бренда в кириллическое.
    #    Кириллическое написание оставляем как есть.
    text = _RE_BRAND_LATIN.sub(_CYRILLIC_BRAND, text)

    # 4. Заменяем плейсхолдеры на пустые линии.
    #    Сначала «ООО [ORG_1]» и подобные конструкции — чтобы не осталось
    #    голого «ООО ___________________________» с висящим организационным
    #    префиксом.
    text = _RE_OOO_PLACEHOLDER.sub(_BLANK_LINE, text)
    text = _RE_QUOTED_PLACEHOLDER.sub(_BLANK_LINE, text)
    text = _RE_PLACEHOLDER.sub(_BLANK_LINE, text)

    # 5. Схлопываем лишние пустые строки, образовавшиеся после удаления блоков.
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 6. Убираем висящие пробелы в конце строк.
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    return text.strip()


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


def _build_shapka_flowables(styles: dict[str, object]) -> list:
    """Сформировать «шапку» документа — пустые поля для ручного заполнения.

    Поля размещаются в правом верхнем углу листа:
        - ФИО заявителя
        - адрес проживания
        - контактный телефон

    Пользователь заполняет их от руки перед подачей в суд или гос. орган.
    Никаких подписей сервиса, логотипов или идентификаторов сессии.
    """
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    # Ширина правой колонки «шапки» — около 85 мм (≈ 1/3 ширины A4 с полями).
    shapka_width = 85 * mm

    # Каждая строка: подпись поля + пустая линия для заполнения от руки.
    rows = [
        [Paragraph(_escape_markup("ФИО:"), styles["shapka_label"]),
         Paragraph(_escape_markup("________________________________"), styles["shapka_line"])],
        [Paragraph(_escape_markup("Адрес:"), styles["shapka_label"]),
         Paragraph(_escape_markup("________________________________"), styles["shapka_line"])],
        [Paragraph(_escape_markup("Телефон:"), styles["shapka_label"]),
         Paragraph(_escape_markup("________________________________"), styles["shapka_line"])],
    ]

    table = Table(
        rows,
        colWidths=[shapka_width * 0.30, shapka_width * 0.70],
        hAlign="RIGHT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    return [table, Spacer(1, 6 * mm)]


def build_pdf(
    content_markup: str,
    include_shapka: bool = True,
) -> bytes:
    """Сформировать чистый PDF-файл и вернуть его содержимое в виде байтов.

    Документ формируется как чистый бланк без брендинга, заголовков сервиса,
    идентификаторов сессий и дисклеймеров ИИ. В правом верхнем углу
    размещается «шапка» с пустыми полями (ФИО, адрес, телефон), которые
    пользователь заполняет от руки перед подачей.

    Args:
        content_markup: основной текст документа (простой markdown).
        include_shapka: добавлять ли «шапку» с пустыми полями в правом
            верхнем углу (по умолчанию — да).

    Returns:
        Байты готового PDF-документа.

    Raises:
        RuntimeError: если сборка документа упала с невосстановимой ошибкой
            (например, файл шрифта недоступен или повреждён).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    started = time.perf_counter()
    logger.info("[PDF] Старт сборки чистого документа")
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
            title="Документ",
            author="",
            subject="",
            creator="",
        )

        flowables: list = []
        if include_shapka:
            flowables.extend(_build_shapka_flowables(styles))
        # Очищаем текст от брендинга, дисклеймеров и плейсхолдеров ПДн
        # перед рендерингом — это гарантирует, что в PDF не попадёт ни
        # латинское «ClickJurist», ни «Генеративный ИИ…», ни «[ORG_1]».
        sanitized_markup = _sanitize_content(content_markup)
        flowables.extend(_markdown_to_flowables(sanitized_markup, styles))
        logger.info(
            "[PDF] Текст документа подготовлен для Canvas (%s flowables)",
            len(flowables),
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
            "[PDF] СБОЙ СБОРКИ через %s мс: %s: %s",
            elapsed_ms, type(exc).__name__, exc,
        )
        # Поднимаем дальше — пусть эндпоинт вернёт 500 за миллисекунды,
        # а не держит соединение открытым бесконечно.
        raise


def filename_for(service: str) -> str:
    """Сформировать безопасное имя PDF-файла для скачивания."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    safe_service = "".join(ch for ch in service if ch.isalnum() or ch in "-_") or "doc"
    return f"document-{safe_service}-{stamp}.pdf"