"""STAGE_3 — обратная подстановка ПДн в сгенерированном документе (152-ФЗ).

Контекст:

    На STAGE_1 контур маскировки заменяет персональные данные клиента
    (ФИО, адреса, телефоны, e-mail, паспорт, ИНН/ОГРН/СНИЛС, реквизиты
    счёта, банковской карты, наименования организаций) стандартизированными
    плейсхолдерами вида ``[ФИО_1]``, ``[АДРЕС_1]``, ``[ТЕЛЕФОН_1]`` и т.д.
    Все эти замены хранятся в карте :attr:`WizardSession.masking_metadata`
    и отправляются во внешнюю LLM вместе с обезличенным текстом — чтобы
    модель не получала ПДн клиента, но понимала структуру документа.

    На STAGE_3 внешняя модель возвращает уже юридически выверенный Markdown
    документа, но **с теми же плейсхолдерами** вместо реальных данных.
    Чтобы выдать клиенту готовый документ, бэкенд должен подставить
    обратно реальные ПДн из ``masking_metadata``.

Безопасность:

    * :func:`deanonymize_document` используется ТОЛЬКО при подтверждённой
      оплате (``session.is_paid == True``). До оплаты клиент видит
      превью только из публичной части документа — это контролируется
      на уровне эндпоинта ``GET /api/wizard/state``.
    * Если по какой-то причине ``masking_metadata`` пуста или повреждена —
      функция возвращает исходный текст без замен (best-effort), без
      падения. Это безопасно: документ останется с масками и клиент
      сможет повторить попытку.
    * Плейсхолдер, для которого нет подстановки, остаётся в тексте как
      есть: явный сигнал оператору/саппорту, что что-то пошло не так.

Соглашения о формате ``masking_metadata``:

    Карта приходит из STAGE_1 и хранится в ``WizardSession.masking_metadata``.
    Формат — нормализованный JSON-словарь, ключи соответствуют
    :data:`backend.services.pii_masker.PLACEHOLDER_KEYS`:

    .. code-block:: json

        {
          "ФИО_1": "Иванов Иван Иванович",
          "АДРЕС_1": "г. Москва, ул. Тверская, д. 1, кв. 23",
          "ТЕЛЕФОН_1": "+7 (905) 123-45-67",
          "EMAIL_1": "client@example.ru",
          ...
        }

    Альтернативный (вложенный) формат, который иногда приходит из LLM на STAGE_1:

    .. code-block:: json

        {
          "ФИО": ["Иванов Иван Иванович", "Петров Пётр"],
          "АДРЕС": ["г. Москва, ..."]
        }

    :func:`deanonymize_document` нормализует оба варианта к плоскому
    ``{placeholder_token: real_value}``.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

logger = logging.getLogger("clickjurist.core.anonymizer")

# ------------------------------------------------------------------------------
# Регулярные выражения
# ------------------------------------------------------------------------------

# Маски вида ``[ФИО_1]``, ``[АДРЕС_12]``, ``[ТЕЛЕФОН_3]`` и т.п.
# Имя типа сущности — кириллица/латиница; число — от 0 до ``[A-Z_]`` не идёт.
# Один и тот же regex покрывает оба семейства — кириллицу и латиницу,
# чтобы быть устойчивым к LLM, иногда «транслитерирующим» теги.
# Захватываем как содержимое в скобках: ``ФИО_1``.
_PLACEHOLDER_RE = re.compile(
    r"\[(?P<token>[A-ZА-ЯЁ][A-ZА-ЯЁ0-9_]*_\d+)\]"
)

# Санити-обрезка для логов: ограничиваем длину значения, чтобы случайно
# не записать в лог гигантскую ПДн.
_LOG_PREVIEW_CHARS = 64


# ------------------------------------------------------------------------------
# Нормализация карты подстановок
# ------------------------------------------------------------------------------
def _flatten_metadata(metadata: dict | None) -> dict[str, str]:
    """Привести ``masking_metadata`` к плоскому виду ``{placeholder: value}``.

    Поддерживаются три варианта входа (все три встречаются в проде):

    1. Плоский — ``{"ФИО_1": "Иванов Иван Иванович", ...}``. Возвращается
       с фильтрацией пустых значений.
    2. По типам — ``{"ФИО": ["Иванов", "Петров"], "АДРЕС": [...]}``.
       Конвертируется в ``{"ФИО_1": "...", "ФИО_2": "...", ...}``. Нумерация
       начинается с 1, чтобы совпадать с плейсхолдерами STAGE_1.
    3. По типам + скаляры — ``{"ФИО": "Иваров", "АДРЕС": [...]}``.
       Одиночное значение трактуется как ``ФИО_1``.

    Любой неподдерживаемый тип значения (int/float/bool) приводится к строке.
    """
    if not metadata or not isinstance(metadata, dict):
        return {}

    result: dict[str, str] = {}

    for raw_key, raw_value in metadata.items():
        if raw_key is None or raw_value is None:
            continue
        key = str(raw_key).strip()
        if not key:
            continue

        if isinstance(raw_value, str):
            value = raw_value.strip()
            if not value:
                continue
            # Если ключ уже в «плейсхолдер-формате» (ФИО_1) — кладём как есть.
            # Иначе — считаем ключом типа сущности и дописываем ``_1``.
            target_key = key if "_" in key and re.search(r"_\d+$", key) else f"{key}_1"
            # Не перетираем уже существующее значение (берём первое вхождение).
            result.setdefault(target_key, value)
            continue

        if isinstance(raw_value, (list, tuple)):
            items: Iterable[object] = raw_value
            base_key = key if "_" in key and re.search(r"_\d+$", key) else key
            for idx, item in enumerate(items, start=1):
                if item is None:
                    continue
                str_value = str(item).strip()
                if not str_value:
                    continue
                target_key = base_key if "_" in base_key and re.search(r"_\d+$", base_key) else f"{base_key}_{idx}"
                result.setdefault(target_key, str_value)
            continue

        # Любой другой скаляр (int / float / bool) — приводим к строке.
        str_value = str(raw_value).strip()
        if str_value:
            target_key = key if "_" in key and re.search(r"_\d+$", key) else f"{key}_1"
            result.setdefault(target_key, str_value)

    return result


# ------------------------------------------------------------------------------
# Публичный API
# ------------------------------------------------------------------------------
def deanonymize_document(
    markdown_text: str,
    metadata: dict | None,
) -> str:
    """Подставить реальные ПДн вместо плейсхолдеров ``[ФИО_1]`` и т.п.

    Функция **неизменна** относительно ``markdown_text``, если
    ``metadata`` пуста или повреждена — это безопасный best-effort: лучше
    показать документ с масками, чем уронить страницу клиента. Плейсхолдеры,
    для которых в карте нет подстановки, остаются в тексте нетронутыми —
    это сигнал для оператора, что-то пошло не так в STAGE_1 (например,
    LLM вернула плейсхолдеры нестандартного формата).

    Args:
        markdown_text: текст документа STAGE_3 (с плейсхолдерами).
        metadata: ``masking_metadata`` сессии визарда.

    Returns:
        Текст документа, где плейсхолдеры заменены реальными значениями.
        Возвращается тот же ``markdown_text``, если в нём нет ни одного
        совпадения с шаблоном плейсхолдера или ``metadata`` пуста.
    """
    if not markdown_text:
        return markdown_text or ""

    replacements = _flatten_metadata(metadata)
    if not replacements:
        # Логируем на уровне DEBUG: в проде это штатный путь (например, если
        # в STAGE_1 ПДн вообще не нашлись и плейсхолдеров в тексте нет).
        logger.debug(
            "deanonymize_document: карта подстановок пуста — текст возвращается без замен."
        )
        return markdown_text

    # Считаем фактическое число замен — для логов и аудита 152-ФЗ.
    applied = 0
    missing_tokens: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        nonlocal applied
        token = match.group("token")
        value = replacements.get(token)
        if value is None:
            missing_tokens.add(token)
            return match.group(0)
        applied += 1
        return value

    try:
        result = _PLACEHOLDER_RE.sub(_replace, markdown_text)
    except Exception as exc:  # noqa: BLE001 — страховка от битого regex-а
        logger.warning(
            "deanonymize_document: ошибка при подстановке плейсхолдеров: %s. "
            "Возвращаем исходный текст без замен.",
            exc,
        )
        return markdown_text

    if missing_tokens:
        # Не критично, но сигнал оператору: плейсхолдеры без подстановки.
        logger.warning(
            "deanonymize_document: найдены плейсхолдеры без подстановки: %s "
            "(applied=%d, missing=%d). Возможно, маппинг неполный — STAGE_1 "
            "мог использовать другой шаблон имён.",
            sorted(missing_tokens)[:10],
            applied,
            len(missing_tokens),
        )

    return result