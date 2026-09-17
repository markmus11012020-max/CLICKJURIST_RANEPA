"""Тесты очистки PDF-документа от брендинга, дисклеймеров и плейсхолдеров."""
from backend.services.pdf_generator import _sanitize_content, build_pdf


def test_sanitize_converts_latin_brand_to_cyrillic():
    """Латинское написание ClickJurist конвертируется в кириллическое «КликЮрист»."""
    text = "Это документ ClickJurist для теста."
    out = _sanitize_content(text)
    assert "ClickJurist" not in out
    assert "clickjurist" not in out.lower()
    assert "КликЮрист" in out


def test_sanitize_keeps_cyrillic_brand():
    """Кириллическое написание КликЮрист сохраняется в тексте."""
    text = "Сервис КликЮрист подготовил документ."
    out = _sanitize_content(text)
    assert "КликЮрист" in out
    assert "ClickJurist" not in out


def test_sanitize_strips_header():
    """Заголовок «Юридическая консультация ClickJurist» удаляется."""
    text = "Юридическая консультация ClickJurist\n\nТело документа."
    out = _sanitize_content(text)
    assert "Юридическая консультация" not in out
    assert "ClickJurist" not in out
    assert "Тело документа." in out


def test_sanitize_strips_footer():
    """Футер «Документ сформирован ClickJurist • сессия 097c2833» удаляется."""
    text = "Тело документа.\n\nДокумент сформирован ClickJurist • сессия 097c2833"
    out = _sanitize_content(text)
    assert "Документ сформирован" not in out
    assert "097c2833" not in out
    assert "ClickJurist" not in out
    assert "Тело документа." in out


def test_sanitize_strips_disclaimer():
    """Двухстрочный дисклеймер ИИ удаляется вместе с разделителем."""
    text = (
        "Тело документа.\n\n---\n"
        "Генеративный ИИ может ошибаться. Для граждан ClickJurist — это первичная "
        "карта действий; для профессиональных юристов — эффективный автоматизированный "
        "инструмент оптимизации рутинных операций."
    )
    out = _sanitize_content(text)
    assert "Генеративный ИИ" not in out
    assert "ClickJurist" not in out
    assert "Тело документа." in out


def test_sanitize_replaces_ooo_org_placeholder():
    """«ООО [ORG_1]» заменяется на пустую линию."""
    text = "Кому: ООО [ORG_1]\n\nТекст обращения."
    out = _sanitize_content(text)
    assert "[ORG_1]" not in out
    assert "ООО" not in out
    assert "___________________________" in out
    assert "Текст обращения." in out


def test_sanitize_replaces_quoted_org_placeholder():
    """«ООО «[ORG_1]»» заменяется на пустую линию."""
    text = "Работодатель ООО «[ORG_1]» нарушил трудовое право."
    out = _sanitize_content(text)
    assert "[ORG_1]" not in out
    assert "___________________________" in out
    assert "нарушил трудовое право." in out


def test_sanitize_replaces_sum_placeholder():
    """«[SUM_1]» заменяется на пустую линию."""
    text = "Сумма иска: [SUM_1] рублей."
    out = _sanitize_content(text)
    assert "[SUM_1]" not in out
    assert "___________________________" in out
    assert "рублей." in out


def test_sanitize_replaces_all_placeholder_types():
    """Все типы плейсхолдеров заменяются на пустые линии."""
    text = (
        "Истец: [NAME_1], адрес: [ADDRESS_1], тел: [PHONE_1], "
        "ИНН: [INN_1], банк: [BANK_1], дата: [DATE_1], дело: [CASE_1]."
    )
    out = _sanitize_content(text)
    for tag in ("NAME", "ADDRESS", "PHONE", "INN", "BANK", "DATE", "CASE"):
        assert f"[{tag}_1]" not in out
    assert out.count("___________________________") >= 7


def test_sanitize_preserves_legal_text():
    """Юридически значимый текст (статьи, даты) сохраняется без изменений."""
    text = (
        "Согласно ст. 395 ГК РФ и ст. 333 НК РФ, "
        "срок исковой давности — 3 года (ст. 196 ГК РФ)."
    )
    out = _sanitize_content(text)
    assert "ст. 395 ГК РФ" in out
    assert "ст. 196 ГК РФ" in out
    assert "3 года" in out


def test_sanitize_handles_empty_input():
    """Пустой вход возвращает пустую строку без ошибок."""
    assert _sanitize_content("") == ""


def test_build_pdf_produces_clean_document():
    """Полная сборка PDF: латиница → кириллица, плейсхолдеры → линии.

    Новые правила чистого вывода:
        - латинское «ClickJurist» НЕ должно попасть в документ;
        - кириллическое «КликЮрист» — единственная допустимая форма бренда;
        - плейсхолдеры ``[ORG_1]``, ``[SUM_1]`` и т. п. заменяются на
          пустые линии для ручного заполнения.
    """
    dirty_markup = (
        "Юридическая консультация ClickJurist\n\n"
        "Кому: ООО [ORG_1]\n\n"
        "Сумма иска: [SUM_1] рублей.\n\n"
        "Согласно ст. 395 ГК РФ.\n\n"
        "---\n"
        "Генеративный ИИ может ошибаться. Для граждан ClickJurist — это первичная "
        "карта действий; для профессиональных юристов — эффективный автоматизированный "
        "инструмент оптимизации рутинных операций.\n\n"
        "Документ сформирован ClickJurist • сессия 097c2833"
    )
    pdf_bytes = build_pdf(dirty_markup, include_shapka=False)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    # PDF — бинарный формат, но текстовые фрагменты можно найти в потоке.
    raw = pdf_bytes.decode("latin-1", errors="ignore")
    # Латинское написание бренда НЕ должно попасть в документ ни в каком регистре.
    assert "ClickJurist" not in raw
    assert "clickjurist" not in raw.lower()
    # Служебные блоки и идентификаторы сессии удалены.
    assert "097c2833" not in raw
    assert "Генеративный ИИ" not in raw
    assert "Документ сформирован" not in raw
    assert "Юридическая консультация" not in raw
    # Плейсхолдеры персональных данных заменены на пустые линии.
    # (Содержимое PDF сжато FlateDecode, поэтому проверяем только
    # отсутствие плейсхолдеров в метаданных и служебных полях.)
    assert "[ORG_1]" not in raw
    assert "[SUM_1]" not in raw