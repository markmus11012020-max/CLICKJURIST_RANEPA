"""Конфигурация ClickJurist Production.

Единая точка чтения настроек из окружения (`.env` для локальной разработки)
и из **Yandex Key Management Service** для production (152-ФЗ, раздел 2 ТЗ).

Порядок загрузки:
    1. `backend.yandex_kms.load_and_apply_secrets()` — если включён KMS,
       расшифровывает секреты и подмешивает их в `os.environ`.
    2. `Settings()` — pydantic-settings читает итоговое окружение.

Раздел 4 ТЗ требует держать конфигурацию Robokassa (`test=1`) именно здесь —
это реализовано классом :class:`RobokassaSettings` и функцией
:func:`robokassa_settings` внизу файла. HTTP-логика эквайринга — в
`backend/services/robokassa.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Пути проекта -------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
SECRETS_DIR = PROJECT_ROOT / "secrets"


def _bootstrap_kms_secrets() -> None:
    """Расшифровать секреты из Yandex KMS до создания объекта Settings.

    Импорт выполняется "лениво" и обёрнут в try/except, чтобы приложение
    поднималось даже при отсутствии пакета KMS (локальная разработка).
    """
    try:
        from backend.yandex_kms import load_and_apply_secrets
    except Exception as exc:  # pragma: no cover — защита от битой установки
        print(f"[WARNING] Модуль Yandex KMS недоступен: {exc}", file=sys.stderr)
        return
    load_and_apply_secrets()


_bootstrap_kms_secrets()


class Settings(BaseSettings):
    """Все переменные окружения проекта ClickJurist Production."""

    # --- 1. Приложение --------------------------------------------------------
    APP_ENV: Literal["development", "production"] = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_BASE_URL: str = "http://localhost:8000"
    LOG_LEVEL: str = "INFO"
    CORS_ALLOW_ORIGINS: str = ""
    """Список разрешённых Origin через запятую.

    Пусто (по умолчанию) — CORS не нужен вовсе: сайт и API живут на
    одном домене. Раньше здесь стоял ``*``, что вместе с JWT-cookie
    (``allow_credentials=True``) означало доступ к API с любого сайта
    из интернета: браузер всё равно не пускает ``*`` с credentials, но
    полагаться на это нельзя.

    Для отдельного фронтенда укажите домен явно:
    ``CORS_ALLOW_ORIGINS=https://site.ru,https://www.site.ru``.
    """

    # --- 1a. Dev Mode: обход платёжного барьера ------------------------------
    # Если ``True`` — backend НИКОГДА не возвращает 402 Payment Required,
    # все запросы пропускаются без списания бесплатного лимита и без
    # проверки оплаты. Используется для локальной отладки сложных
    # юридических сценариев и нагрузочного тестирования.
    #
    # ВНИМАНИЕ: значение по умолчанию — ``False``. Раньше здесь стояло
    # ``True``, из-за чего незадание переменной окружения молча отключало
    # оплату в production. Включать обход нужно ЯВНО:
    #   DEV_BYPASS_PAYWALL=true  (только локально или в CI)
    DEV_BYPASS_PAYWALL: bool = False

    # --- 2. 152-ФЗ: идентификация сессии без персональных данных -------------
    SESSION_HASH_SALT: str = "please-change-this-salt"
    FREE_TIER_REQUESTS: int = 0

    # --- Anti-abuse: rate-limit на дорогие эндпоинты ------------------------
    # Дополняет free-tier и оплату: даже платный пользователь не должен иметь
    # возможность слать 1000 запросов в минуту (скрипт с украденной сессией
    # или «зомби»-расширение). Два независимых лимита:
    #   1) per-session — на один браузер/сессию. Окно 1 час. Защищает от
    #      накрутки конкретным пользователем.
    #   2) per-IP — на один IP. Окно 1 минута. Защищает от ботнета, который
    #      перебирает новые session_hash, чтобы получить free-trial.
    # При срабатывании — 429 Too Many Requests с заголовком ``Retry-After``.
    RATE_LIMIT_SESSION_PER_HOUR: int = 30
    RATE_LIMIT_IP_PER_MIN: int = 20

    # Бесплатный лимит на уровне сети (один IP), поверх ``FREE_TIER_REQUESTS``.
    #
    # Отключён вместе с ``FREE_TIER_REQUESTS`` (октябрь 2026): бесплатный
    # trial снят с продакшна, чтобы исключить вектор abuse при росте трафика.
    # Поля сохранены как резерв — при возврате trial-режима достаточно задать
    # положительные значения. Зачем второй уровень исторически: ``session_hash``
    # строится из отпечатка браузера (заголовок ``X-Client-Fingerprint``),
    # который можно подменить — без сетевого счётчика лимит обходится сменой
    # заголовка. Значение задаётся конфигурацией и обязано быть >=
    # ``FREE_TIER_REQUESTS`` (контролируется ``startup_checks.py``).
    FREE_TIER_IP_REQUESTS: int = 0

    # --- 3. Хранилище --------------------------------------------------------
    DATABASE_URL: str = "sqlite:///./data/clickjurist.db"

    # --- 4. Stage 1: контур маскировки ПДн -----------------------------------
    MASKING_PROVIDER: Literal["yandex", "ollama", "regex"] = "yandex"
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"
    OLLAMA_TIMEOUT_S: int = 120
    YANDEX_GPT_URL: str = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    YANDEX_API_KEY: str = ""
    YANDEX_IAM_TOKEN: str = ""
    YANDEX_FOLDER_ID: str = ""
    YANDEX_GPT_MODEL: str = "yandexgpt-lite"
    YANDEX_TIMEOUT_S: int = 120

    # --- 5. Stage 2: AITunnel ------------------------------------------------
    ROUTER_API_KEY: str = ""
    ROUTER_BASE_URL: str = "https://api.aitunnel.ru/v1"
    ROUTER_DEFAULT_MODEL: str = "gemini-2.5-flash"
    MODEL_LLM_1: str = "deepseek-v4-flash"
    MODEL_LLM_2: str = "minimax-m3"
    MODEL_GEMINI_FACTCHECK: str = "gemini-2.5-flash"
    AITUNNEL_TIMEOUT_S: int = 600
    MAX_TOKENS_LLM_1: int = 4000
    MAX_TOKENS_LLM_2: int = 6000
    # Расширенные лимиты для длинных юридических документов и чек-листов,
    # чтобы модель не обрывала генерацию искового заявления / претензии
    # / жалобы / 4-шагового чек-листа с локализованными судами и сроками.
    MAX_TOKENS_DOCUMENT: int = 8000
    MAX_TOKENS_CHECKLIST: int = 4000

    # --- 6. Failover-оркестратор ---------------------------------------------
    PRIMARY_PROVIDER: Literal["aitunnel", "yandex"] = "aitunnel"

    # --- 7. Web-фактчекинг (2–3 независимых источника) -----------------------
    WEB_SEARCH_PROVIDER: Literal["none", "yandex", "serper", "tavily"] = "none"
    ENABLE_WEB_SEARCH: bool = True
    WEB_SEARCH_MIN_SOURCES: int = 2
    WEB_SEARCH_MAX_SOURCES: int = 3
    YANDEX_SEARCH_API_KEY: str = ""
    YANDEX_SEARCH_URL: str = "https://searchapi.api.cloud.yandex.net/v2/web/search"
    SERPER_API_KEY: str = ""
    SERPER_URL: str = "https://google.serper.dev/search"
    TAVILY_API_KEY: str = ""
    TAVILY_URL: str = "https://api.tavily.com/search"

    # --- 8. Yandex Key Management Service ------------------------------------
    YANDEX_KMS_ENABLED: bool = False
    YANDEX_KMS_URL: str = "https://kms.api.cloud.yandex.net/kms/v1/keys"
    YANDEX_KMS_KEY_ID: str = ""
    YANDEX_KMS_CIPHERTEXT_FILE: str = "secrets/kms-secrets.b64"
    YANDEX_KMS_REFRESH_INTERVAL_S: int = 300

    # --- 9. Zero-Storage Logging (Yandex Cloud Logging) ----------------------
    YANDEX_LOGGING_ENABLED: bool = False
    YANDEX_LOGGING_URL: str = "https://logging.api.cloud.yandex.net/logging/v1/entries:write"
    YANDEX_LOG_GROUP_ID: str = ""

    # --- 10. Robokassa -------------------------------------------------------
    ROBOKASSA_LOGIN: str = "clickjurist"
    ROBOKASSA_PASSWORD1: str = "test_password_1"
    ROBOKASSA_PASSWORD2: str = "test_password_2"
    ROBOKASSA_TEST: bool = True
    ROBOKASSA_PAYMENT_URL: str = "https://auth.robokassa.ru/Merchant/Index.aspx"
    ROBOKASSA_HASH_ALGORITHM: Literal["md5", "sha256"] = "md5"
    ROBOKASSA_SUCCESS_URL: str = "/api/payment/success"
    ROBOKASSA_FAIL_URL: str = "/api/payment/fail"
    ROBOKASSA_RESULT_URL: str = "/api/payment/result"

    # --- 11. Тарифы ----------------------------------------------------------
    # Снижены в ~2 раза от прежних (99/100/300/390). Себестоимость одного
    # запроса упала кратно (LLM-контур стал локальным), поэтому розница
    # пересмотрена: 49 / 50 / 150 ₽ за отдельные шаги, 195 ₽ за пакет.
    PRICE_CONSULTATION: int = 49
    PRICE_CHECKLIST: int = 50
    PRICE_DOCUMENT: int = 150
    # Пакетный тариф «Решение проблемы под ключ» (раздел 6 ТЗ prompt160926.md).
    # Тарифов ровно один: «премиум»-уровень из ТЗ не был реализован — он
    # предлагался ботом, но не принимался API и не отображался на сайте.
    PRICE_PACKAGE_BASIC: int = 195

    # --- 12. PDF -------------------------------------------------------------
    PDF_FONT_PATH: str = ""

    # --- 13. JWT-авторизация (раздел 3.1 ТЗ prompt160926.md) -----------------
    JWT_SECRET: str = "please-change-this-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_TTL_DAYS: int = 30
    JWT_COOKIE_NAME: str = "cj_session"
    JWT_COOKIE_SECURE: bool = False  # True в production (HTTPS)
    JWT_COOKIE_SAMESITE: str = "strict"

    # --- 14. Фоновые задачи (раздел 2.1 ТЗ prompt160926.md) ------------------
    # Режим: "memory" (локально) или "celery" (production с Redis).
    TASK_BACKEND: Literal["memory", "celery"] = "memory"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    TASK_RESULT_TTL_S: int = 3600  # Хранить результат 1 час

    # --- 14a. Wizard: гибридное хранилище состояний (STAGE_1) -----------------
    # Горячий кэш визарда — Redis (production) или in-process dict (dev/тесты).
    # Режим ``memory`` включается и тогда, когда ``TASK_BACKEND==memory`` —
    # иначе в dev-окружении без Redis всё ломается на ровном месте.
    WIZARD_HOT_BACKEND: Literal["memory", "redis"] = "memory"
    WIZARD_REDIS_URL: str = "redis://localhost:6379/2"
    # TTL «горячей» записи визарда (секунды). Если пользователь ушёл с сайта
    # посередине шага — запись протухнет сама, без ручной сборки мусора.
    # 24 часа — компромисс между UX (можно вернуться завтра) и режимом
    # No-Data-Retention (сырой текст ПДн не залёживается).
    WIZARD_HOT_TTL_S: int = 24 * 60 * 60
    # Префикс ключа в Redis — чтобы не пересекаться с Celery-пространством.
    WIZARD_REDIS_PREFIX: str = "cj:wizard:"
    # Период фонового GC для in-memory backend (секунды).
    WIZARD_GC_INTERVAL_S: int = 300

    # --- 15. Веб-фактчекинг: приоритетные домены (раздел 2.2 ТЗ) ------------
    PRIORITY_DOMAINS: str = "consultant.ru,garant.ru,pravo.gov.ru,sozd.duma.gov.ru"
    WEB_FETCH_TIMEOUT_S: int = 10
    WEB_FETCH_MAX_CHARS: int = 4000

    # --- 16. Guardrails: контроль галлюцинаций (раздел 5.2 ТЗ) ---------------
    GUARDRAILS_ENABLED: bool = True
    GUARDRAILS_MAX_RETRIES: int = 2

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Утилиты -------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        """True, если приложение запущено в production-окружении."""
        return self.APP_ENV == "production"

    @property
    def is_sqlite(self) -> bool:
        """True, если используется локальное SQLite-хранилище."""
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def cors_origins(self) -> list[str]:
        """Список Origin для CORS (строка через запятую).

        Пустое значение означает «только свой домен»: это безопасный режим
        по умолчанию, потому что сайт и API обычно на одном хосте.
        """
        raw = (self.CORS_ALLOW_ORIGINS or "").strip()
        if not raw or raw == "*":
            return [self.app_origin]
        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def cors_credentials(self) -> bool:
        """Разрешены ли credentials (JWT-cookie) для CORS-запросов.

        При ``*`` отправка credentials запрещена: так требует спецификация
        Fetch. Иначе брокер не отдаст ``Access-Control-Allow-Origin: *`` и
        запрос с cookie просто не пройдёт.
        """
        raw = (self.CORS_ALLOW_ORIGINS or "").strip()
        return raw not in {"", "*"}

    @property
    def app_origin(self) -> str:
        """Origin самого приложения — ``scheme://host`` из ``APP_BASE_URL``."""
        base = (self.APP_BASE_URL or "http://localhost:8000").rstrip("/")
        return "/".join(base.split("/", 3)[:3])

    @property
    def prices(self) -> dict[str, int]:
        """Тарифы в рублях по кодам платных услуг.

        Тариф ``package_premium`` упразднён (на сайте продаётся только пакет
        за 195 ₽), поэтому в публичный прайс он не попадает: иначе бот и
        ``/api/prices`` предлагали бы услугу, которой нельзя оплатить.
        """
        return {
            "consultation": self.PRICE_CONSULTATION,
            "checklist": self.PRICE_CHECKLIST,
            "document": self.PRICE_DOCUMENT,
            "pdf": self.PRICE_DOCUMENT,
            "package_basic": self.PRICE_PACKAGE_BASIC,
        }

    @property
    def priority_domains_list(self) -> list[str]:
        """Список приоритетных доменов для веб-фактчекинга (раздел 2.2 ТЗ)."""
        return [d.strip().lower() for d in self.PRIORITY_DOMAINS.split(",") if d.strip()]


settings = Settings()


# ------------------------------------------------------------------------------
# Robokassa SDK — конфигурационный слой (раздел 4 ТЗ)
# ------------------------------------------------------------------------------
class RobokassaSettings(BaseModel):
    """Параметры подключения к Робокассе (sandbox: ``test=1``).

    Значения берутся из :data:`settings`, т.е. из окружения / Yandex KMS.
    Никаких сетевых вызовов здесь нет — это чистый конфигурационный слой.
    """

    login: str
    password1: str
    password2: str
    test: bool = True
    payment_url: str = "https://auth.robokassa.ru/Merchant/Index.aspx"
    hash_algorithm: str = "md5"
    success_url: str = "/api/payment/success"
    fail_url: str = "/api/payment/fail"
    result_url: str = "/api/payment/result"

    @property
    def test_param(self) -> int:
        """Параметр ``IsTest`` для платёжной формы: 1 — песочница, 0 — бой."""
        return 1 if self.test else 0

    def absolute(self, path_or_url: str) -> str:
        """Привести относительный путь возврата к абсолютному URL."""
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        base = settings.APP_BASE_URL.rstrip("/")
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{base}{path_or_url}"


def robokassa_settings() -> RobokassaSettings:
    """Собрать актуальные настройки Робокассы из окружения."""
    return RobokassaSettings(
        login=settings.ROBOKASSA_LOGIN,
        password1=settings.ROBOKASSA_PASSWORD1,
        password2=settings.ROBOKASSA_PASSWORD2,
        test=settings.ROBOKASSA_TEST,
        payment_url=settings.ROBOKASSA_PAYMENT_URL,
        hash_algorithm=settings.ROBOKASSA_HASH_ALGORITHM,
        success_url=settings.ROBOKASSA_SUCCESS_URL,
        fail_url=settings.ROBOKASSA_FAIL_URL,
        result_url=settings.ROBOKASSA_RESULT_URL,
    )
