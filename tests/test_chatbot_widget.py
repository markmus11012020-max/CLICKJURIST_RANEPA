"""Регрессионные тесты виджета: опечатки в полях и разметка.

Польза этого файла — в проверке того, что не ловится обычными тестами
Python. Ошибка ``this._state.set(...)`` вместо ``this.state.set(...)``
не проходит никакой синтаксической проверки, но в браузере роняет
подписчика события: кнопка остановки не появляется, а ошибка
маскируется остальными — внешне это выглядит как «виджет сломан».

Поэтому здесь статический разбор исходников виджета: ищем обращения
к полям, которых в классе нет.
"""
from __future__ import annotations

import re
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent / "chatbot" / "static" / "js"


def _sources() -> list[Path]:
    return sorted(JS_DIR.rglob("*.js"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _own_members(path: Path) -> set[str]:
    """Поля и методы текущего класса.

    Поля объявляются как ``this.x = ...``, методы — как ``  name(`` с учётом
    модификаторов ``static`` / ``async`` / ``get`` / ``set``.
    """
    source = _read(path)
    members = set(re.findall(r"this\.(\w+)\s*=", source))
    members.update(
        re.findall(r"^\s{2}(?:static\s+|async\s+|get\s+|set\s+)*(\w+)\s*\(", source, re.M)
    )
    return members


def test_widget_sources_are_readable() -> None:
    """Исходники виджета читаются как UTF-8 и не содержат битых символов."""
    files = _sources()
    assert files, "исходники виджета не найдены"
    for path in files:
        text = _read(path)
        assert text.strip(), f"{path} пуст"
        assert "�" not in text, f"{path} содержит повреждённые символы"


def test_no_emoji_in_avatar_markup() -> None:
    """Эмблема виджета — SVG, а не эмодзи.

    Эмодзи весов ⚖ в системных шрифтах рисуется двумя «мешочками» и
    выглядит как сломанная картинка.
    """
    panel = _read(JS_DIR / "ui" / "ChatPanel.js")
    avatar = re.search(r'<span class="cj-avatar".*?</span>', panel, re.S)
    assert avatar, "не найден блок эмблемы cj-avatar"
    assert "<svg" in avatar.group(0), "эмблема должна быть inline-SVG"
    assert "⚖" not in panel, "эмодзи весов в шапке виджета не убран"


def test_no_undeclared_field_access_in_widget() -> None:
    """Ни один класс не обращается к полю, которое в нём не объявлено.

    Ловит опечатки вида ``this._state`` в классе, где поле называется
    ``state``: синтаксис корректен, но в браузере будет TypeError. Именно
    так падал подписчик ``stream:meta`` — кнопка остановки не
    появлялась, а ошибка маскировалась.
    """
    offenders: list[str] = []
    for path in _sources():
        source = _read(path)
        own = _own_members(path)
        if not own:
            continue
        for field in sorted(set(re.findall(r"this\.(\w+)", source))):
            if field in own or field in {"location", "ClickJuristChatBot"}:
                continue
            offenders.append(f"{path.relative_to(JS_DIR)}: this.{field}")

    assert not offenders, "обращения к необъявленным полям: " + ", ".join(offenders)


def test_composer_reenables_input_when_loading_finishes() -> None:
    """Composer снимает блокировку поля, когда ``isLoading`` становится false.

    Пользователь жаловался, что «Напишите вопрос» невозможно набрать: если
    флаг не сбросится, textarea остаётся disabled навсегда.
    """
    composer = _read(JS_DIR / "ui" / "Composer.js")
    block = re.search(r"state\.subscribe\(\(current\)\s*=>\s*\{(.*?)\n\s*\}\);", composer, re.S)
    assert block, "Composer должен подписываться на состояние"
    body = block.group(1)
    assert "isLoading" in body, "подписка должна реагировать на isLoading"
    assert "_syncDisabled" in body, (
        "при сбросе isLoading поле обязано разблокироваться через _syncDisabled()"
    )


def test_streaming_bubble_used_for_incremental_text() -> None:
    """Лента рисует «живой» пузырь во время стриминга."""
    message_list = _read(JS_DIR / "ui" / "MessageList.js")
    assert "StreamingBubble" in message_list, "лента обязана использовать StreamingBubble"
    for hook in ("startStream", "appendDelta", "finishStream"):
        assert hook in message_list, f"в ленте нет обработчика {hook}"

    bubble = _read(JS_DIR / "ui" / "StreamingBubble.js")
    assert "is-streaming" in bubble, (
        "живой пузырь должен помечаться классом is-streaming — по нему включается курсор"
    )
    assert "append(" in bubble, "пузырь должен уметь дописывать фрагменты"


def test_stream_client_parses_sse_incrementally() -> None:
    """Клиент разбирает кадры по мере поступления, а не в конце потока."""
    client = _read(JS_DIR / "services" / "StreamClient.js")
    assert "getReader()" in client, "нужен потоковый reader"
    assert "FRAME_DELIMITER" in client, "кадры разделяются пустой строкой"
    for case in ("meta", "delta", "done", "error"):
        assert f"'{case}'" in client, f"клиент не обрабатывает событие {case}"
