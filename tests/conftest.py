"""Pytest-фикстуры для ClickJurist Production."""

import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# httpx этой версии логирует HTTP-ответ как
#   logger.info('HTTP Request: %s %s "%s %d %s"', ..., '200', ...)
# — код ответа приходит строкой при формате «%d». Стандартный Formatter
# на такой записи падает с TypeError, а pytest превращает ошибку
# форматирования в падение теста. Лог запросов в тестах не нужен.
logging.getLogger("httpx").setLevel(logging.WARNING)

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SESSION_HASH_SALT", "test-salt")
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test_clickjurist.db")
os.environ.setdefault("FREE_TIER_REQUESTS", "1")
os.environ.setdefault("FREE_TIER_IP_REQUESTS", "5")
os.environ.setdefault("MASKING_PROVIDER", "regex")
os.environ.setdefault("ROUTER_API_KEY", "")
os.environ.setdefault("YANDEX_FOLDER_ID", "")
os.environ.setdefault("YANDEX_API_KEY", "")
os.environ.setdefault("ENABLE_WEB_SEARCH", "false")

# В тестах режим разработчика должен быть ВЫКЛЮЧЕН — иначе сломается
# test_session_gate_returns_402_after_free_tier_consumed и другие регрессии
# платёжного барьера. Дефолт в Settings — False, но фиксируем его явно,
# чтобы тесты не зависели от локального .env.
os.environ.setdefault("DEV_BYPASS_PAYWALL", "false")


@pytest.fixture(autouse=True)
def reset_db():
    """Пересоздавать БД перед каждым тестом (полная изоляция)."""
    from backend.db import store

    if store.dialect == "sqlite":
        path = Path(store._sqlite_path)
        if path.exists():
            path.unlink()
    store.init_schema()

    # Wizard-худ-кеш — singleton. Без явной очистки состояние сессий
    # протекает между тестами (например, is_paid прошлого кейса влияет
    # на новый). Сбрасываем и HOT, и COLD.
    from backend import wizard_store

    wizard_store.reset_for_tests()

    yield
