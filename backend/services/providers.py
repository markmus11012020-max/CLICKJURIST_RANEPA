"""LLM-провайдеры ClickJurist Production.

Реализованы три независимых поставщика моделей:

======================  ==========================================  ==============================
Провайдер               Транспорт                                   Роль в пайплайне
======================  ==========================================  ==============================
``AITunnelProvider``    OpenAI-совместимый шлюз (Gemini/Minimax)     Stage 2 + асессор
``YandexGPTProvider``   Yandex Foundation Models (облако РФ)         Stage 1 + failover Stage 2
``OllamaProvider``      Локальный инстанс (полная изоляция)          Stage 1
======================  ==========================================  ==============================

Любая сетевая аномалия, таймаут или HTTP 5xx превращается в
:class:`LLMError` — именно на этом исключении строится failover-оркестратор
(раздел 3 ТЗ, `backend/services/llm_chain.py`).
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod

import requests

from backend.config import settings


class LLMError(RuntimeError):
    """Единая ошибка обращения к LLM-провайдеру (сеть, таймаут, HTTP 5xx)."""


# Паттерн «не-ASCII» — используется для защиты HTTP-заголовков. Библиотека
# ``requests`` кодирует заголовки в Latin-1 (RFC 2616), поэтому любая
# кириллица или иной non-ASCII символ роняет запрос с UnicodeEncodeError.
# Чтобы этого не происходило, _ascii_safe() принудительно фильтрует
# значения заголовков до отправки.
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")


def _ascii_safe(label: str, value: str | None, provider: str) -> str:
    """Вернуть строку, безопасную для HTTP-заголовков (только ASCII).

    Любые не-ASCII символы (включая кириллицу) вырезаются, попутно пишется
    WARNING в журнал — чтобы было видно, откуда пришла «грязная» строка.
    Пустые / ``None`` значения приводятся к пустой строке.

    Args:
        label: имя поля (например, ``Authorization`` или ``X-Folder-Id``).
        value: исходное значение.
        provider: имя провайдера для журнала.

    Returns:
        Значение, гарантированно состоящее только из ASCII-символов.
    """
    if value is None:
        return ""
    cleaned = str(value).strip()
    if not cleaned:
        return ""
    if _NON_ASCII_RE.search(cleaned):
        # Никогда не отправляем кириллицу / эмодзи в HTTP-заголовки.
        # requests использует Latin-1, поэтому UnicodeEncodeError здесь
        # вылетает ДО отправки запроса и валит весь pipeline.
        import logging

        logging.getLogger("clickjurist.providers").warning(
            "В заголовке %s для провайдера %s обнаружены не-ASCII символы; "
            "удаляю их, чтобы не нарваться на UnicodeEncodeError",
            label,
            provider,
        )
        cleaned = _NON_ASCII_RE.sub("", cleaned)
        # После вырезания кириллицы мог остаться «пустой» пробел
        # (например: "Привет мир" → " "). Убираем его — пустой заголовок
        # лучше, чем заголовок из одних пробелов.
        cleaned = cleaned.strip()
    return cleaned


def _sanitize_headers(headers: dict[str, str], provider: str) -> dict[str, str]:
    """Прогнать словарь заголовков через ``_ascii_safe``.

    Возвращает НОВЫЙ словарь — оригинал не мутируется, чтобы поведение
    было предсказуемым в тестах и при повторных вызовах.
    """
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        safe_key = str(key).strip() or "X-Unknown-Header"
        sanitized[safe_key] = _ascii_safe(safe_key, value, provider)
    return sanitized


def _post_json(
    url: str, headers: dict[str, str], payload: dict, timeout: int, provider: str
) -> dict:
    """Выполнить POST-запрос и вернуть JSON, превратив любые сбои в LLMError."""
    # Санитизируем заголовки ДО запроса. Если что-то пошло не так (например,
    # в настройки попало значение с кириллицей) — мы увидим это в логах, а
    # requests не упадёт с UnicodeEncodeError.
    safe_headers = _sanitize_headers(headers, provider)
    try:
        response = requests.post(url, headers=safe_headers, json=payload, timeout=timeout)
    except UnicodeEncodeError as exc:
        # requests использует Latin-1 для HTTP-заголовков; кириллица в
        # заголовках даёт именно это исключение. Превращаем в LLMError,
        # чтобы _call_with_failover мог переключиться на резерв.
        raise LLMError(
            f"{provider}: недопустимые символы в HTTP-заголовках "
            f"(вероятно кириллица) — {exc}"
        ) from exc
    except requests.Timeout as exc:
        raise LLMError(f"{provider}: таймаут {timeout} с — {exc}") from exc
    except requests.RequestException as exc:
        raise LLMError(f"{provider}: сетевая ошибка — {exc}") from exc
    except ValueError as exc:
        # requests иногда бросает ValueError при проблемах с подготовкой
        # запроса (например, при некорректных URL). Тоже считаем LLMError.
        raise LLMError(f"{provider}: некорректный запрос — {exc}") from exc

    if response.status_code >= 500:
        raise LLMError(f"{provider}: сервер вернул HTTP {response.status_code}")
    if response.status_code >= 400:
        raise LLMError(
            f"{provider}: HTTP {response.status_code} — {response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise LLMError(f"{provider}: ответ не является JSON — {exc}") from exc


class BaseProvider(ABC):
    """Базовый интерфейс LLM-провайдера."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Готов ли провайдер к работе (заданы ли ключи/адреса)."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> str:
        """Выполнить чат-запрос и вернуть текст ответа модели."""

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> "Iterator[str]":
        """Стриминг токенов от модели (Шаг 1 ТЗ prompt170926.md).

        По умолчанию провайдеры НЕ поддерживают стриминг — выбрасывается
        :class:`LLMError`. Конкретные реализации (AITunnel, YandexGPT, Ollama)
        переопределяют этот метод, если их API поддерживает SSE/stream-режим.
        """
        raise LLMError(f"{self.name}: стриминг токенов не поддерживается этим провайдером")

    def __repr__(self) -> str:  # pragma: no cover — отладочное представление
        return f"<{self.__class__.__name__} name={self.name}>"


class AITunnelProvider(BaseProvider):
    """Провайдер AITunnel: Gemini 2.5 Flash, Minimax и другие модели."""

    name = "aitunnel"
    # Сколько раз повторять запрос, если провайдер вернул content: null
    RETRY_ON_NULL = 2
    RETRY_SLEEP_S = 5

    def is_configured(self) -> bool:
        """True, если задан ключ маршрутизатора AITunnel."""
        return bool(settings.ROUTER_API_KEY.strip())

    @property
    def endpoint(self) -> str:
        """Полный URL эндпоинта Chat Completions."""
        return f"{settings.ROUTER_BASE_URL.rstrip('/')}/chat/completions"

    def chat(
    self,
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    ) -> str:
        """Отправить запрос в AITunnel (OpenAI-совместимый протокол).

        Когда ``model`` равен ``None`` (например, при failover на резервный
        провайдер), используется :attr:`ROUTER_DEFAULT_MODEL` — иначе API
        получит ``"model": null`` и вернёт 400.
        """
        if not self.is_configured():
            raise LLMError("aitunnel: не задан ROUTER_API_KEY")

        model = model or settings.ROUTER_DEFAULT_MODEL
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {_ascii_safe('ROUTER_API_KEY', settings.ROUTER_API_KEY, self.name)}",
            "Content-Type": "application/json",
        }

        last_error = "пустой ответ"
        for attempt in range(self.RETRY_ON_NULL + 1):
            data = _post_json(
                self.endpoint,
                headers,
                payload,
                settings.AITUNNEL_TIMEOUT_S,
                self.name,
            )
            choices = data.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content")
                if content and str(content).strip():
                    return str(content).strip()
                last_error = "провайдер вернул content: null"

            if attempt < self.RETRY_ON_NULL:
                time.sleep(self.RETRY_SLEEP_S * (attempt + 1))

        raise LLMError(f"aitunnel: {last_error} после {self.RETRY_ON_NULL + 1} попыток")

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ):
        """Стриминг токенов через AITunnel (OpenAI-совместимый SSE).

        Возвращает генератор строк — фрагментов текста ответа модели.
        Использует ``stream: true`` в payload и читает NDJSON из HTTP-ответа.
        """
        if not self.is_configured():
            raise LLMError("aitunnel: не задан ROUTER_API_KEY")

        model = model or settings.ROUTER_DEFAULT_MODEL
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {_ascii_safe('ROUTER_API_KEY', settings.ROUTER_API_KEY, self.name)}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        try:
            response = requests.post(
                self.endpoint,
                headers=_sanitize_headers(headers, self.name),
                json=payload,
                timeout=settings.AITUNNEL_TIMEOUT_S,
                stream=True,
            )
        except requests.RequestException as exc:
            raise LLMError(f"aitunnel: сетевая ошибка — {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(
                f"aitunnel: HTTP {response.status_code} — {response.text[:300]}"
            )

        # Читаем SSE-поток: строки вида "data: {...}\n\n", терминатор "data: [DONE]".
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except ValueError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield str(piece)


class YandexGPTProvider(BaseProvider):
    """Провайдер YandexGPT (Yandex Foundation Models, контур РФ).

    Используется в двух ролях:
        * Stage 1 — маскировка персональных данных внутри контура РФ;
        * failover-ветка Stage 2 — когда AITunnel недоступен (раздел 3 ТЗ).
    """

    name = "yandex"

    def is_configured(self) -> bool:
        """True, если задан folder_id и хотя бы один способ авторизации.

        Дополнительно проверяем, что ключи и folder_id — чистый ASCII,
        иначе мы гарантированно получим UnicodeEncodeError при отправке
        HTTP-заголовков в Yandex Cloud API.
        """
        folder_id = _ascii_safe("YANDEX_FOLDER_ID", settings.YANDEX_FOLDER_ID, self.name)
        api_key = _ascii_safe("YANDEX_API_KEY", settings.YANDEX_API_KEY, self.name)
        iam_token = _ascii_safe("YANDEX_IAM_TOKEN", settings.YANDEX_IAM_TOKEN, self.name)
        has_auth = bool(api_key or iam_token)
        return bool(folder_id) and has_auth

    def _auth_header(self) -> str:
        """Собрать заголовок авторизации: API-ключ приоритетнее IAM-токена.

        Возвращаемое значение ВСЕГДА состоит только из ASCII-символов
        (Latin letters, digits и разделители ``-``, ``_``, ``.``).
        Никакой кириллицы здесь быть не должно — иначе ``requests`` упадёт
        с ``UnicodeEncodeError`` ещё до отправки запроса.
        """
        api_key = _ascii_safe("YANDEX_API_KEY", settings.YANDEX_API_KEY, self.name)
        if api_key:
            return f"Api-Key {api_key}"
        try:
            from backend.yandex_kms import get_iam_token

            token = get_iam_token()
            if token:
                safe_token = _ascii_safe("IAM_TOKEN", token, self.name)
                if safe_token:
                    return f"Bearer {safe_token}"
        except Exception:  # pragma: no cover
            pass
        iam_token = _ascii_safe("YANDEX_IAM_TOKEN", settings.YANDEX_IAM_TOKEN, self.name)
        return f"Bearer {iam_token}"

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> str:
        """Отправить запрос в YandexGPT (протокол Foundation Models).

        ВАЖНО: русский текст промпта (поле ``text`` в ``messages``)
        уходит СТРОГО в теле запроса (``json=payload``). В заголовки
        попадают только ``Authorization`` и ``Content-Type`` — оба
        гарантированно ASCII после ``_sanitize_headers``.
        """
        if not self.is_configured():
            raise LLMError("yandex: не заданы YANDEX_FOLDER_ID и ключ доступа")

        # Folder id и имя модели тоже идут в URL — но мы их всё равно
        # санитизируем здесь для единообразия и защиты от мусорных значений
        # в .env (например, если кто-то случайно оставит комментарий
        # на кириллице рядом с folder_id).
        folder_id = _ascii_safe("YANDEX_FOLDER_ID", settings.YANDEX_FOLDER_ID, self.name)
        model_id = _ascii_safe("YANDEX_GPT_MODEL", model or settings.YANDEX_GPT_MODEL, self.name)
        if not folder_id or not model_id:
            raise LLMError(
                "yandex: некорректные YANDEX_FOLDER_ID/YANDEX_GPT_MODEL "
                "(пустые или содержат не-ASCII символы)"
            )

        payload = {
            "modelUri": f"gpt://{folder_id}/{model_id}",
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": max_tokens,
            },
            "messages": [
                {
                    "role": "system" if item.get("role") == "system" else "user",
                    "text": str(item.get("content", "")),
                }
                for item in messages
            ],
        }
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
        }
        data = _post_json(
            settings.YANDEX_GPT_URL,
            headers,
            payload,
            settings.YANDEX_TIMEOUT_S,
            self.name,
        )
        alternatives = ((data.get("result") or {}).get("alternatives")) or []
        if not alternatives:
            raise LLMError("yandex: в ответе нет alternatives")
        text = ((alternatives[0].get("message")) or {}).get("text", "")
        if not text or not str(text).strip():
            raise LLMError("yandex: пустой текст ответа модели")
        return str(text).strip()


class OllamaProvider(BaseProvider):
    """Провайдер локального Ollama — полностью изолированный контур Stage 1."""

    name = "ollama"

    def is_configured(self) -> bool:
        """True, если задан адрес локального инстанса Ollama."""
        return bool(settings.OLLAMA_BASE_URL.strip())

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> str:
        """Отправить запрос в локальный Ollama (``/api/chat``)."""
        url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
        payload = {
            "model": model or settings.OLLAMA_MODEL,
            "messages": [
                {"role": item.get("role", "user"), "content": str(item.get("content", ""))}
                for item in messages
            ],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        data = _post_json(url, {}, payload, settings.OLLAMA_TIMEOUT_S, self.name)
        content = (data.get("message") or {}).get("content", "")
        if not content or not str(content).strip():
            raise LLMError("ollama: пустой ответ модели")
        return str(content).strip()


# --- Реестр провайдеров -------------------------------------------------------
def get_provider(name: str) -> BaseProvider:
    """Вернуть экземпляр провайдера по имени (``aitunnel`` | ``yandex`` | ``ollama``)."""
    registry: dict[str, type[BaseProvider]] = {
        "aitunnel": AITunnelProvider,
        "yandex": YandexGPTProvider,
        "ollama": OllamaProvider,
    }
    provider_cls = registry.get(name.strip().lower())
    if provider_cls is None:
        raise LLMError(
            f"Неизвестный LLM-провайдер '{name}'. Доступно: {sorted(registry)}"
        )
    return provider_cls()


def fallback_chain() -> list[str]:
    """Порядок обхода провайдеров: основной из настроек, затем резервный.

    Bidirectional-логика (раздел 3 ТЗ): если ``PRIMARY_PROVIDER=aitunnel``,
    резерв — YandexGPT, и наоборот.
    """
    primary = settings.PRIMARY_PROVIDER
    secondary = "yandex" if primary == "aitunnel" else "aitunnel"
    return [primary, secondary]