"""Тесты безопасности и псевдонимизации (раздел 2 ТЗ)."""
from backend.security import (
    anonymized_session_id,
    client_fingerprint,
    compute_session_hash,
    hash_inv_id,
)


def test_session_hash_is_pseudonymous():
    """Один и тот же клиент всегда получает один и тот же хеш."""
    ip = "203.0.113.42"
    fp = "abc123"
    h1 = compute_session_hash(ip, fp)
    h2 = compute_session_hash(ip, fp)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256


def test_session_hash_changes_with_salt():
    """Изменение соли даёт другой хеш (нельзя восстановить данные)."""
    h1 = compute_session_hash("1.2.3.4", "fp")
    from backend import config as cfg
    original = cfg.settings.SESSION_HASH_SALT
    cfg.settings.SESSION_HASH_SALT = "another-salt"
    h2 = compute_session_hash("1.2.3.4", "fp")
    cfg.settings.SESSION_HASH_SALT = original
    assert h1 != h2


def test_anonymized_uuid_is_stable():
    """UUID анонимизации стабилен для одного хеша сессии."""
    h = compute_session_hash("1.2.3.4", "fp")
    u1 = anonymized_session_id(h)
    u2 = anonymized_session_id(h)
    assert u1 == u2
    assert len(u1) == 36


def test_inv_id_is_hashed():
    """Номера счетов Robokassa хранятся только в виде необратимого хеша."""
    h = hash_inv_id("12345")
    assert len(h) == 32
    assert hash_inv_id("12345") == h
    assert hash_inv_id("99999") != h


def test_client_fingerprint_fallback(monkeypatch):
    """Без заголовка отпечаток вычисляется из User-Agent."""
    class _R:
        headers = {"user-agent": "TestAgent/1.0", "accept-language": "ru-RU"}
        client = type("C", (), {"host": "127.0.0.1"})()
    fp = client_fingerprint(_R())  # type: ignore[arg-type]
    assert isinstance(fp, str) and len(fp) > 0
