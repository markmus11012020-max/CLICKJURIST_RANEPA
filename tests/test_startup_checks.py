"""Тесты проверки стартовой конфигурации.

Модуль не даёт подняться в production с обходом оплаты или с секретами
по умолчанию: лучше явная ошибка при старте, чем бесплатный доступ и
подделываемые сессии в проде.
"""
from __future__ import annotations

import pytest

from backend import config, startup_checks


@pytest.fixture()
def settings_factory(monkeypatch):
    """Подменить значения Settings и пересобрать объект."""

    def _make(**overrides):
        base = {
            "APP_ENV": "production",
            "DEV_BYPASS_PAYWALL": False,
            "TASK_BACKEND": "celery",
            "SESSION_HASH_SALT": "salt-ok",
            "JWT_SECRET": "jwt-ok",
            "YANDEX_KMS_ENABLED": True,
            "JWT_COOKIE_SECURE": True,
            "CORS_ALLOW_ORIGINS": "https://clickjurist.ru",
        }
        base.update(overrides)
        for key, value in base.items():
            monkeypatch.setattr(config.settings, key, value, raising=False)
        return config.settings

    return _make


def test_development_has_no_issues(settings_factory) -> None:
    settings_factory(APP_ENV="development", DEV_BYPASS_PAYWALL=True)
    assert startup_checks.collect_production_issues() == []


def test_healthy_production_config_passes(settings_factory) -> None:
    settings_factory()
    assert startup_checks.collect_production_issues() == []


def test_paywall_bypass_is_critical(settings_factory) -> None:
    settings_factory(DEV_BYPASS_PAYWALL=True)
    issues = startup_checks.collect_production_issues()
    assert any("DEV_BYPASS_PAYWALL" in i for i in issues)
    with pytest.raises(RuntimeError, match="DEV_BYPASS_PAYWALL"):
        startup_checks.validate_startup()


def test_default_salt_is_critical(settings_factory) -> None:
    settings_factory(SESSION_HASH_SALT="please-change-this-salt")
    with pytest.raises(RuntimeError, match="SESSION_HASH_SALT"):
        startup_checks.validate_startup()


def test_default_jwt_secret_is_critical(settings_factory) -> None:
    settings_factory(JWT_SECRET="please-change-this-jwt-secret")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        startup_checks.validate_startup()


def test_memory_task_backend_is_warning_only(settings_factory) -> None:
    """Потеря фоновых задач — предупреждение, а не повод не подниматься."""
    settings_factory(TASK_BACKEND="memory")
    issues = startup_checks.validate_startup()
    assert any("TASK_BACKEND" in i for i in issues)


def test_weak_production_flags_are_warnings(settings_factory) -> None:
    settings_factory(
        YANDEX_KMS_ENABLED=False,
        JWT_COOKIE_SECURE=False,
        CORS_ALLOW_ORIGINS="*",
    )
    issues = startup_checks.validate_startup()
    assert len(issues) == 3
