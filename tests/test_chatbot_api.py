"""Тесты HTTP-слоя чат-бота: контракт /api/chatbot."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot.app import register
from chatbot.container import ChatbotContainer
from chatbot.config import ChatbotSettings
from chatbot.llm.gateway import NullLLMGateway
from chatbot.repositories.in_memory import InMemoryConversationRepository
from chatbot.services.orchestrator import ChatOrchestrator
from chatbot.knowledge.base import StaticKnowledgeBase

SESSION = "test-session-0001"


@pytest.fixture()
def client() -> TestClient:
    """Тестовое приложение с изолированным контейнером (без LLM)."""
    settings = ChatbotSettings(llm_enabled=False, auto_open=True)
    container = ChatbotContainer(settings=settings)
    container.llm = NullLLMGateway()
    container.orchestrator = ChatOrchestrator(
        repository=InMemoryConversationRepository(max_conversations=5),
        knowledge=StaticKnowledgeBase(),
        llm=NullLLMGateway(),
        max_message_length=200,
    )

    app = FastAPI()
    register(app, container=container, mount_static=False)
    return TestClient(app)


def test_config_endpoint(client: TestClient) -> None:
    payload = client.get("/api/chatbot/config").json()
    assert payload["enabled"] is True
    assert payload["brand"] == "КликЮрист"
    assert "disclaimer" in payload


def test_greeting_endpoint(client: TestClient) -> None:
    payload = client.get("/api/chatbot/greeting", params={"session_id": SESSION}).json()
    assert "КликЮрист" in payload["text"]
    assert len(payload["quick_replies"]) >= 3
    assert payload["is_returning"] is False
    assert payload["session_id"] == SESSION


def test_message_endpoint_answers_pricing(client: TestClient) -> None:
    response = client.post(
        "/api/chatbot/message",
        json={"session_id": SESSION, "message": "сколько стоит?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "49" in body["reply"]["text"]
    assert body["reply"]["intent"] == "pricing"
    assert body["turn_count"] == 1


def test_message_with_intent_hint(client: TestClient) -> None:
    response = client.post(
        "/api/chatbot/message?intent_hint=privacy",
        json={"session_id": SESSION, "message": "подробнее"},
    )
    assert "152-ФЗ" in response.json()["reply"]["text"]


def test_history_endpoint(client: TestClient) -> None:
    client.get("/api/chatbot/greeting", params={"session_id": SESSION})
    body = client.get("/api/chatbot/history", params={"session_id": SESSION}).json()
    assert len(body["messages"]) >= 1


def test_reset_endpoint_clears_history(client: TestClient) -> None:
    client.get("/api/chatbot/greeting", params={"session_id": SESSION})
    client.post("/api/chatbot/reset", params={"session_id": SESSION})
    body = client.get("/api/chatbot/history", params={"session_id": SESSION}).json()
    assert body["messages"] == []


def test_health_endpoint(client: TestClient) -> None:
    body = client.get("/api/chatbot/health").json()
    assert body["status"] == "ok"
    assert body["llm_available"] is False
    assert body["knowledge_articles"] >= 9


def test_invalid_session_id_rejected(client: TestClient) -> None:
    # Слишком короткий — отсекает валидатор схемы (422).
    assert client.get("/api/chatbot/greeting", params={"session_id": "ab"}).status_code == 422
    # Допустимая длина, но недопустимые символы — отсекает SessionIdentity (400).
    assert (
        client.get("/api/chatbot/greeting", params={"session_id": "bad id;drop"}).status_code
        == 400
    )


def test_empty_message_rejected(client: TestClient) -> None:
    """Пробелы проходят min_length, но отсекаются оркестратором (400)."""
    response = client.post(
        "/api/chatbot/message", json={"session_id": SESSION, "message": "  "}
    )
    assert response.status_code == 400
    assert "detail" in response.json()


def test_blank_session_id_rejected_by_schema(client: TestClient) -> None:
    response = client.post("/api/chatbot/message", json={"session_id": "", "message": "привет"})
    assert response.status_code == 422


def test_disabled_module_returns_503() -> None:
    container = ChatbotContainer(settings=ChatbotSettings(enabled=False, llm_enabled=False))
    app = FastAPI()
    register(app, container=container, mount_static=False)
    disabled = TestClient(app)

    response = disabled.get("/api/chatbot/greeting", params={"session_id": SESSION})
    assert response.status_code == 503
    # Конфигурация и здоровье остаются доступны — чтобы видеть причину.
    assert disabled.get("/api/chatbot/health").json()["enabled"] is False


def test_session_ids_are_hashed_server_side(client: TestClient) -> None:
    """Сырой идентификатор не должен попадать в ключ хранилища."""
    client.get("/api/chatbot/greeting", params={"session_id": SESSION})
    client.post(
        "/api/chatbot/message", json={"session_id": SESSION, "message": "сколько стоит?"}
    )
    body = client.get("/api/chatbot/health").json()
    assert body["conversations"] == 1
    # В ответе наружу отдаётся исходный id — он и есть публичный ключ.
    assert body["llm_provider"] == "none"
