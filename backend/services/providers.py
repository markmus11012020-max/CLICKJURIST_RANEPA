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

import time
from abc import ABC, abstractmethod

import requests

from backend.config import settings


class LLMError(RuntimeError):
    """Единая ошибка обращения к LLM-провайдеру (сеть, таймаут, HTTP 5xx)."""


def _post_json(
    url: str, headers: dict[str, str], payload: dict, timeout: int, provider: str
) -> dict:
    """Выполнить POST-запрос и вернуть JSON, превратив любые сбои в LLMError."""
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise LLMError(f"{provider}: таймаут {timeout} с — {exc}") from exc
    except requests.RequestException as exc:
        raise LLMError(f"{provider}: сетевая ошибка — {exc}") from exc

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
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> str:
        """Отправить запрос в AITunnel (OpenAI-совместимый протокол)."""
        if not self.is_configured():
            raise LLMError("aitunnel: не задан ROUTER_API_KEY")

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {settings.ROUTER_API_KEY.strip()}",
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


class YandexGPTProvider(BaseProvider):
    """Провайдер YandexGPT (Yandex Foundation Models, контур РФ).

    Используется в двух ролях:
        * Stage 1 — маскировка персональных данных внутри контура РФ;
        * failover-ветка Stage 2 — когда AITunnel недоступен (раздел 3 ТЗ).
    """

    name = "yandex"

    def is_configured(self) -> bool:
        """True, если задан folder_id и хотя бы один способ авторизации."""
        has_auth = bool(settings.YANDEX_API_KEY.strip() or settings.YANDEX_IAM_TOKEN.strip())
        return bool(settings.YANDEX_FOLDER_ID.strip()) and has_auth

    def _auth_header(self) -> str:
        """Собрать заголовок авторизации: API-ключ приоритетнее IAM-токена."""
        api_key = settings.YANDEX_API_KEY.strip()
        if api_key:
            return f"Api-Key {api_key}"
        try:
            from backend.yandex_kms import get_iam_token

            token = get_iam_token()
            if token:
                return f"Bearer {token}"
        except Exception:  # pragma: no cover
            pass
        return f"Bearer {settings.YANDEX_IAM_TOKEN.strip()}"

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> str:
        """Отправить запрос в YandexGPT (протокол Foundation Models)."""
        if not self.is_configured():
            raise LLMError("yandex: не заданы YANDEX_FOLDER_ID и ключ доступа")

        model_id = model or settings.YANDEX_GPT_MODEL
        payload = {
            "modelUri": f"gpt://{settings.YANDEX_FOLDER_ID.strip()}/{model_id}",
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