"""Интеграция с Yandex Key Management Service (KMS).

Раздел 2 ТЗ: «Все API-ключи, креды БД и окружения (`.env`) должны управляться
и шифроваться через Yandex KMS».

Схема работы:
    1. В облаке секреты шифруются (``yc kms symmetric-crypto encrypt``),
       результат (base64) кладётся в ``secrets/kms-secrets.b64``
       или в переменную окружения ``YANDEX_KMS_CIPHERTEXT_B64``.
    2. При старте :func:`load_and_apply_secrets` расшифровывает blob
       через KMS API и подмешивает пары ключ-значение в ``os.environ``.
    3. :class:`backend.config.Settings` читает уже готовое окружение.

Безопасность:
    * значения секретов никогда не логируются (только имена ключей);
    * подмешиваются только ключи из белого списка :data:`SECRET_ENV_KEYS`;
    * при любой ошибке приложение не падает — печатается предупреждение
      и используются значения локального ``.env``.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

import requests

# --- Белый список ключей, которые разрешено подмешивать из KMS ---------------
SECRET_ENV_KEYS: frozenset[str] = frozenset(
    {
        "ROUTER_API_KEY",
        "YANDEX_API_KEY",
        "YANDEX_IAM_TOKEN",
        "DATABASE_URL",
        "SESSION_HASH_SALT",
        "ROBOKASSA_LOGIN",
        "ROBOKASSA_PASSWORD1",
        "ROBOKASSA_PASSWORD2",
        "YANDEX_SEARCH_API_KEY",
        "SERPER_API_KEY",
        "TAVILY_API_KEY",
        "YANDEX_FOLDER_ID",
        "YANDEX_KMS_KEY_ID",
    }
)

METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)

_lock = threading.Lock()
_last_load_ts: float = 0.0
_last_payload: dict[str, str] = {}


def _env_flag(name: str, default: bool = False) -> bool:
    """Прочитать булев флаг из окружения."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Прочитать целое из окружения с безопасным значением по умолчанию."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_iam_token() -> str:
    """Получить IAM-токен для вызовов Yandex Cloud API.

    Приоритет источников:
        1. ``YANDEX_IAM_TOKEN`` — явный токен (удобно для локальной отладки);
        2. сервисный аккаунт Compute Cloud — через metadata-сервис;
        3. пустая строка, если токен получить не удалось.
    """
    explicit = (os.getenv("YANDEX_IAM_TOKEN") or "").strip()
    if explicit:
        return explicit
    try:
        resp = requests.get(
            METADATA_TOKEN_URL,
            headers={"Metadata-Flavor": "Yandex"},
            timeout=3,
        )
        if resp.status_code == 200:
            return str(resp.json().get("access_token", "")).strip()
    except requests.RequestException:
        pass
    return ""


def _read_ciphertext_b64() -> str:
    """Прочитать base64-шифротекст KMS из переменной окружения или файла."""
    inline = (os.getenv("YANDEX_KMS_CIPHERTEXT_B64") or "").strip()
    if inline:
        return inline

    path = (os.getenv("YANDEX_KMS_CIPHERTEXT_FILE") or "").strip()
    if not path:
        return ""
    if not os.path.isabs(path):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, path)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def decrypt_secrets(ciphertext_b64: str) -> dict[str, str]:
    """Расшифровать blob секретов через KMS API.

    Args:
        ciphertext_b64: base64 от результата ``Encrypt`` в Yandex KMS.

    Returns:
        Словарь секретов (ключ → значение). Пустой словарь при любой ошибке.
    """
    key_id = (os.getenv("YANDEX_KMS_KEY_ID") or "").strip()
    if not key_id:
        print("[WARNING] YANDEX_KMS_KEY_ID не задан — KMS пропущен")
        return {}

    iam = get_iam_token()
    if not iam:
        print("[WARNING] Не удалось получить IAM-токен для Yandex KMS")
        return {}

    base_url = os.getenv(
        "YANDEX_KMS_URL", "https://kms.api.cloud.yandex.net/kms/v1/keys"
    ).rstrip("/")
    url = f"{base_url}/{key_id}:decrypt"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {iam}",
                "Content-Type": "application/json",
            },
            json={"keyId": key_id, "ciphertext": ciphertext_b64},
            timeout=15,
        )
        resp.raise_for_status()
        plaintext = base64.b64decode(resp.json().get("plaintext", "")).decode("utf-8")
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            print("[WARNING] KMS вернул не JSON-объект — секреты не применены")
            return {}
        return {str(key): str(value) for key, value in data.items()}
    except requests.RequestException as exc:
        print(f"[WARNING] Ошибка вызова Yandex KMS: {exc}")
    except (ValueError, TypeError) as exc:
        print(f"[WARNING] Не удалось разобрать секреты из KMS: {exc}")
    return {}


def apply_secrets(secrets: dict[str, str]) -> list[str]:
    """Подмешать секреты в окружение (только ключи из белого списка).

    Returns:
        Список имён применённых ключей (без значений!).
    """
    applied: list[str] = []
    for key, value in secrets.items():
        if key in SECRET_ENV_KEYS and value:
            os.environ[key] = value
            applied.append(key)
    return applied


def load_and_apply_secrets(force: bool = False) -> list[str]:
    """Загрузить секреты из KMS и применить их к окружению.

    Функция идемпотентна: повторные вызовы в пределах интервала
    ``YANDEX_KMS_REFRESH_INTERVAL_S`` используют кэш.

    Args:
        force: игнорировать кэш и перечитать секреты.

    Returns:
        Список имён применённых ключей (пустой, если KMS выключен).
    """
    global _last_load_ts, _last_payload

    if not _env_flag("YANDEX_KMS_ENABLED", False):
        return []

    interval = _env_int("YANDEX_KMS_REFRESH_INTERVAL_S", 300)
    with _lock:
        if not force and _last_payload and (time.time() - _last_load_ts) < interval:
            return apply_secrets(_last_payload)

        ciphertext = _read_ciphertext_b64()
        if not ciphertext:
            print("[WARNING] Шифротекст KMS не найден — используются значения .env")
            return []

        secrets = decrypt_secrets(ciphertext)
        if not secrets:
            return []

        _last_payload = secrets
        _last_load_ts = time.time()
        applied = apply_secrets(secrets)
        print(
            f"[OK] Yandex KMS: применено секретов — {len(applied)} "
            f"({', '.join(applied)})"
        )
        return applied