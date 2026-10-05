"""Проверка конфигурации перед стартом сервера.

Задача модуля — не допустить тихой потери денег и данных из-за
неправильной конфигурации в production. Все проверки собирают список
проблем и либо поднимают исключение (критично), либо пишут в лог
(предупреждение).
"""
from __future__ import annotations

import logging

from backend.config import settings

logger = logging.getLogger(__name__)


def collect_production_issues() -> list[str]:
    """Собрать список проблем конфигурации (пусто — всё в порядке)."""
    issues: list[str] = []
    if not settings.is_production:
        return issues

    if settings.DEV_BYPASS_PAYWALL:
        issues.append(
            "DEV_BYPASS_PAYWALL=true в production: платёжный барьер отключён, "
            "услуги выдаются бесплатно. Установите DEV_BYPASS_PAYWALL=false."
        )

    if settings.TASK_BACKEND == "memory":
        issues.append(
            "TASK_BACKEND=memory в production: фоновые задачи теряются при "
            "перезапуске и не распределяются между инстансами. "
            "Установите TASK_BACKEND=celery и задайте CELERY_BROKER_URL."
        )

    if settings.SESSION_HASH_SALT == "please-change-this-salt":
        issues.append(
            "SESSION_HASH_SALT не задан: идентификаторы сессий предсказуемы, "
            "любой может подделать чужую сессию."
        )

    if settings.JWT_SECRET == "please-change-this-jwt-secret":
        issues.append(
            "JWT_SECRET не задан: токены сессии можно подделать, авторизация "
            "перестаёт защищать оплаченный доступ."
        )

    if not settings.YANDEX_KMS_ENABLED:
        issues.append(
            "YANDEX_KMS_ENABLED=false: секреты читаются из обычного .env "
            "вместо Yandex Key Management Service."
        )

    if not settings.JWT_COOKIE_SECURE:
        issues.append(
            "JWT_COOKIE_SECURE=false: cookie передаётся по HTTP. "
            "В production обязательно true (нужен HTTPS)."
        )

    if settings.cors_origins == ["*"] or settings.CORS_ALLOW_ORIGINS.strip() == "*":
        issues.append(
            "CORS_ALLOW_ORIGINS=*: любой сайт может обращаться к API. "
            "Перечислите домены явно."
        )

    if settings.FREE_TIER_IP_REQUESTS < settings.FREE_TIER_REQUESTS:
        issues.append(
            f"FREE_TIER_IP_REQUESTS={settings.FREE_TIER_IP_REQUESTS} меньше "
            f"FREE_TIER_REQUESTS={settings.FREE_TIER_REQUESTS}: сетевой лимит "
            "исчерпается быстрее браузерного, и реальный бесплатный запрос "
            "станет недоступен в одиночку. Значение должно быть >= "
            "FREE_TIER_REQUESTS."
        )

    return issues


def validate_startup() -> list[str]:
    """Проверить конфигурацию и сообщить о проблемах.

    Критичные проблемы останавливают старт: лучше не подняться вовсе,
    чем работать без оплаты или со скомпрометированными секретами.
    """
    issues = collect_production_issues()
    if not issues:
        return issues

    critical = [
        issue
        for issue in issues
        if "DEV_BYPASS_PAYWALL" in issue
        or "SESSION_HASH_SALT" in issue
        or "JWT_SECRET" in issue
    ]
    warnings = [issue for issue in issues if issue not in critical]

    for issue in warnings:
        logger.warning("Конфигурация: %s", issue)

    if critical:
        joined = "\n  - ".join(critical)
        raise RuntimeError(
            "Критическая ошибка конфигурации в production:\n  - " + joined
        )
    return issues
