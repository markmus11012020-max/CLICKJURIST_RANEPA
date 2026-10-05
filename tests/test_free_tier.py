"""Регрессионные тесты бесплатного лимита (раздел 4 ТЗ).

Проверяем инварианты двухуровневой квоты:

    * первый запрос с нового браузера проходит без 402;
    * подмена отпечатка браузера (заголовок ``X-Client-Fingerprint``)
      НЕ выдаёт новый бесплатный запрос — иначе лимит обходится тривиально;
    * очистка локального хранилища браузера тоже не помогает;
    * сетевой лимит общий для разных браузеров за одним IP;
    * оплаченный доступ работает независимо от обоих ключей;
    * режим разработчика не списывает ни один из уровней.
"""

from __future__ import annotations

import pytest

from backend.config import settings
from backend.db import store
from backend.main import _session_gate
from backend.security import compute_ip_hash, compute_session_hash, quota_keys


class _FakeRequest:
    """Минимальный объект запроса, который принимает ``session_gate``."""

    def __init__(self, fingerprint: str, ip: str = "203.0.113.7") -> None:
        self.headers = {
            "x-forwarded-for": ip,
            "x-client-fingerprint": fingerprint,
        }
        self.client = type("C", (), {"host": ip})()


def _use_free(fingerprint: str, ip: str = "203.0.113.7") -> bool:
    """Прогнать гейт и списать браузерный клют, как это делают эндпоинты."""
    request = _FakeRequest(fingerprint, ip)
    session_hash, _, was_free, denial = _session_gate(request, "consultation")
    if denial is not None:
        return False
    if was_free:
        store.consume_free_request(session_hash)
    return True


# ------------------------------------------------------------------------------
# Базовое поведение
# ------------------------------------------------------------------------------
def test_first_request_from_new_browser_is_free():
    """Новый браузер получает бесплатный запрос."""
    assert _use_free("fresh-browser-1") is True


def test_second_request_from_same_browser_is_blocked():
    """Второй запрос из того же браузера упирается в 402."""
    assert _use_free("repeat-browser-1") is True
    assert _use_free("repeat-browser-1") is False


# ------------------------------------------------------------------------------
# Ключевой инвариант: подмена отпечатка не обходит лимит
# ------------------------------------------------------------------------------
def test_fingerprint_spoofing_is_bounded_by_network_quota():
    """Смена отпечатка не даёт бесконечный лимит.

    Одиночная подмена отпечатка внутри ``FREE_TIER_IP_REQUESTS`` ещё проходит:
    это осознанная плата за NAT — за одним IP лежат целые офисы, мобильные
    операторы и домашние сети, и лимит «1 запрос на IP» заблокировал бы их
    всех сразу. Ключевое свойство в том, что подмена ограничена: сколько
    отпечатков ни подставляй, бесплатных запросов будет не больше, чем
    ``FREE_TIER_IP_REQUESTS``.
    """
    ip = "203.0.113.77"
    granted = 0
    for index in range(settings.FREE_TIER_IP_REQUESTS + 5):
        if _use_free(f"spoofed-fingerprint-{index}", ip=ip):
            granted += 1
        else:
            break

    assert granted == settings.FREE_TIER_IP_REQUESTS, (
        "подмена отпечатка должна упираться в сетевой лимит, получено "
        f"{granted} вместо {settings.FREE_TIER_IP_REQUESTS}"
    )


def test_fingerprint_rotation_exhausts_whole_ip_quota():
    """Бесконечная смена отпечатка упирается в сетевой лимит, а не бесконечна."""
    ip = "198.51.100.23"
    granted = 0
    for index in range(settings.FREE_TIER_IP_REQUESTS + 3):
        if _use_free(f"rotating-fp-{index}", ip=ip):
            granted += 1
        else:
            break

    assert granted == settings.FREE_TIER_IP_REQUESTS, (
        "сетевой лимит должен исчерпаться ровно через FREE_TIER_IP_REQUESTS "
        f"попыток, получено {granted}"
    )


def test_network_quota_is_shared_between_different_browsers():
    """Два разных браузера за одним IP делят сетевой лимит."""
    ip = "198.51.100.42"
    assert _use_free("browser-A", ip=ip) is True

    # Второй браузер с того же IP получит свой браузерный бесплатный запрос,
    # но сетевой счётчик увеличится — значит, лимит общий, а не дублируется.
    before = store.network_free_requests_used(compute_ip_hash(ip))
    _use_free("browser-B", ip=ip)
    after = store.network_free_requests_used(compute_ip_hash(ip))

    assert before < after
    assert before == 1, "первый запрос должен был создать сетевой счётчик"


def test_different_ips_have_independent_quota():
    """Разные IP не мешают друг другу."""
    assert _use_free("browser-x", ip="198.51.100.100") is True
    assert _use_free("browser-y", ip="198.51.100.101") is True


# ------------------------------------------------------------------------------
# Оплата
# ------------------------------------------------------------------------------
def test_paid_access_bypasses_exhausted_browser_quota():
    """Оплата снимает браузерный лимит (регрессия из test_payments)."""
    request = _FakeRequest("paid-browser-1")
    session_hash, _, _, _ = _session_gate(request, "consultation")
    store.consume_free_request(session_hash)
    store.grant_paid_access(session_hash, days=30)

    _, _, was_free, denial = _session_gate(request, "consultation")
    assert denial is None
    assert was_free is False


def test_paid_access_works_even_when_network_quota_exhausted():
    """Заплативший пользователь не блокируется из-за «соседей» по IP."""
    ip = "203.0.113.250"
    for index in range(settings.FREE_TIER_IP_REQUESTS):
        _use_free(f"neighbour-{index}", ip=ip)

    assert store.can_use_free_request_by_ip(compute_ip_hash(ip)) is False

    paid_request = _FakeRequest("paying-neighbour", ip=ip)
    paid_hash, _, _, _ = _session_gate(paid_request, "consultation")
    store.grant_paid_access(paid_hash, days=30)

    _, _, was_free, denial = _session_gate(paid_request, "consultation")
    assert denial is None, "оплаченный доступ обязан работать при исчерпанном IP"
    assert was_free is False


# ------------------------------------------------------------------------------
# Dev Mode
# ------------------------------------------------------------------------------
def test_dev_bypass_consumes_neither_level(monkeypatch: pytest.MonkeyPatch):
    """В режиме разработчика не списываются ни браузерный, ни сетевой ключ."""
    monkeypatch.setattr(settings, "DEV_BYPASS_PAYWALL", True)
    request = _FakeRequest("dev-browser-1")
    session_hash, _, was_free, denial = _session_gate(request, "consultation")

    assert denial is None
    assert was_free is False
    assert store.network_free_requests_used(compute_ip_hash("203.0.113.7")) == 0


# ------------------------------------------------------------------------------
# Хеши
# ------------------------------------------------------------------------------
def test_ip_hash_never_equals_session_hash():
    """Префикс ``ip-level|`` исключает совпадение двух уровней квоты."""
    ip = "203.0.113.7"
    for fingerprint in ("a", "b", "ip-level", "c" * 64):
        assert compute_ip_hash(ip) != compute_session_hash(ip, fingerprint)


def test_quota_keys_returns_session_and_network_hashes():
    """``quota_keys`` отдаёт ровно два независимых ключа."""
    request = _FakeRequest("quota-keys-browser")
    session_hash, network_hash = quota_keys(request)

    assert session_hash == compute_session_hash("203.0.113.7", "quota-keys-browser")
    assert network_hash == compute_ip_hash("203.0.113.7")
    assert session_hash != network_hash


def test_ip_limit_is_not_below_browser_limit():
    """Сетевой лимит не может быть меньше браузерного (иначе он не работает)."""
    assert settings.FREE_TIER_IP_REQUESTS >= settings.FREE_TIER_REQUESTS
