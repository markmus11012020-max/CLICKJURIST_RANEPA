"""Регрессионные тесты подключения стилей виджета.

Здесь ловится ошибка, из-за которой виджет открывался «голым»: модуль
``styles.js`` лежит в ``<static>/js/utils/``, а стили — в ``<static>/css/``.
База URL вычисляется как ``new URL('../../', import.meta.url)``; если
вернуться на один уровень меньше, браузер запросит ``/chatbot/static/js/css/...``
и получит 404 — интерфейс отрисуется без единого правила CSS.

Тесты работают с исходником виджета, а не с собранным бандлом: в проекте
сборщика нет, браузер получает файлы как есть.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "chatbot" / "static"
STYLES_JS = STATIC_DIR / "js" / "utils" / "styles.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_styles_module_uses_two_levels_up():
    """База стилей — на два уровня выше папки модуля, а не на один."""
    source = _read(STYLES_JS)
    match = re.search(r"new URL\('([^']+)',\s*import\.meta\.url\)", source)
    assert match, "styles.js должен вычислять базу от import.meta.url"
    assert match.group(1) == "../../", (
        f"ожидается '../../' из js/utils/ до каталога css/, "
        f"получено {match.group(1)!r}"
    )


def test_declared_style_files_exist():
    """Каждый файл из STYLE_FILES реально лежит в каталоге static/css."""
    source = _read(STYLES_JS)
    files = re.findall(r"'css/([^']+\.css)'", source)
    assert files, "в styles.js должен быть список подключаемых файлов"
    for name in files:
        assert (STATIC_DIR / "css" / name).is_file(), f"нет файла стилей {name}"


def test_widget_markup_has_hooks_for_every_component():
    """Классы, на которые навешены стили, присутствуют в разметке виджета.

    Несовпадение имён здесь означает «мёртвый» CSS: правила есть, а
    элементов, к которым они применяются, нет.

    Классы собираются в разных формах — литералом, конкатенацией по роли
    (``cj-message--${isBot ? 'bot' : 'user'}``) и внутри HTML-строк, — поэтому
    проверяется наличие базового имени, а не точной строки.
    """
    css = _read(STATIC_DIR / "css" / "chatbot.css")
    used_classes = set(re.findall(r"\.(cj-[a-z0-9-]+)", css))
    assert used_classes, "в chatbot.css должны быть селекторы виджета"

    source_files = list((STATIC_DIR / "js").rglob("*.js"))
    assert source_files, "исходники виджета не найдены"
    markup = "\n".join(_read(path) for path in source_files)

    # Модификаторы вида `cj-message--bot` собираются конкатенацией и ternary
    # (`cj-message--${isBot ? 'bot' : 'user'}`), поэтому проверяется корневое
    # имя блока: его наличие означает, что блок действительно используется.
    roots = {name.split("--")[0] for name in used_classes}
    missing = sorted(
        name for name in roots if not re.search(re.escape(name) + r"\b", markup)
    )
    assert not missing, (
        "в разметке нет элементов для классов из CSS: " + ", ".join(missing)
    )
