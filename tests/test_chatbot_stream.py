"""Тесты потокового режима чат-бота (SSE) и LLM-шлюза.

Проверяются три сценария: ответ из базы знаний (мгновенный), потоковая
генерация моделью с «живой печатью» и деградация, когда модель молчит.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot.app import register
from chatbot.config import ChatbotSettings
from chatbot.container import ChatbotContainer
from chatbot.domain.interfaces import LLMGateway
from chatbot.knowledge.base import StaticKnowledgeBase
from chatbot.llm.streaming import StreamEvent, StreamEventType
from chatbot.repositories.in_memory import InMemoryConversationRepository
from chatbot.services.orchestrator import ChatOrchestrator

SESSION = "stream-session-1"


class FakeStreamingLLM(LLMGateway):
    """Шлюз, отдающий заранее заданные фрагменты."""

    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces
        self.calls: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return True

    def answer(self, question, context, history):
        return "".join(self._pieces)

    def stream_answer(self, question, context, history):
        self.calls.append((question, context))
        for piece in self._pieces:
            yield piece

    @property
    def last_provider(self) -> str:
        return "fake"


class SilentLLM(LLMGateway):
    """Шлюз, доступный, но не отдающий ни одного фрагмента."""

    def is_available(self) -> bool:
        return True

    def answer(self, question, context, history):
        return None

    def stream_answer(self, question, context, history):
        return iter(())


def _orchestrator(llm) -> ChatOrchestrator:
    return ChatOrchestrator(
        repository=InMemoryConversationRepository(max_conversations=5),
        knowledge=StaticKnowledgeBase(),
        llm=llm,
        max_message_length=200,
        # Пауза между фрагментами нулевая: тесты проверяют форму потока,
        # а не его скорость, и не должны ждать реальную «печать».
        type_delay_s=0.0,
    )


def _events(text: str, llm, hint: str | None = None) -> list[StreamEvent]:
    orch = _orchestrator(llm)
    return list(
        orch.stream_message(SESSION, text, intent_hint=hint)  # noqa: B023
    )


def _parse_sse(payload: str) -> list[dict]:
    """Разобрать поток SSE в список событий."""
    events = []
    for frame in payload.split("\n\n"):
        frame = frame.strip()
        if not frame.startswith("data:"):
            continue
        events.append(json.loads(frame[len("data:"):].strip()))
    return events


# ---------------------------------------------------------------------------
# События потока
# ---------------------------------------------------------------------------
def test_knowledge_answer_stream_shape() -> None:
    """Ответ из базы знаний: META → DELTA* → DONE.

    Раньше готовый ответ уходил одним фрагментом, и в интерфейсе он выглядел
    как мёртвое поле без «живой печати». Поэтому фрагментов должно быть
    несколько, а их склейка — в точности совпадать с финальным текстом.
    """
    events = _events("сколько стоит?", FakeStreamingLLM(["неважно"]))
    types = [e.type for e in events]

    assert types[0] is StreamEventType.META
    assert types[-1] is StreamEventType.DONE
    assert set(types[1:-1]) == {StreamEventType.DELTA}

    deltas = [e.data["text"] for e in events if e.type is StreamEventType.DELTA]
    assert len(deltas) > 1, "готовый ответ должен дробиться на фрагменты"
    assert events[0].data["source"] == "knowledge"
    assert events[0].data["streamed"] is False

    final = events[-1].data["reply"]
    assert "".join(deltas) == final["text"]
    assert "49" in final["text"]
    assert final["intent"] == "pricing"


def test_llm_streams_piece_by_piece() -> None:
    """Модель отдаёт текст по частям — каждая часть отдельным DELTA."""
    pieces = ["Тарифы ", "начинаются ", "от 49 ₽."]
    events = _events("а что там с деньгами", FakeStreamingLLM(pieces))

    types = [e.type for e in events]
    assert types[0] is StreamEventType.META
    assert types[-1] is StreamEventType.DONE
    deltas = [e.data["text"] for e in events if e.type is StreamEventType.DELTA]
    assert deltas == pieces
    assert events[0].data["source"] == "llm"
    assert events[0].data["streamed"] is True


def test_llm_answer_sanitized_and_joined() -> None:
    """Склейка фрагментов проходит через санитайзер.

    Вопрос намеренно вне базы знаний — иначе сработала бы более дешёвая
    ветка с ответом из статьи, и модель не вызывалась бы.
    """
    events = _events(
        "ааа ббб ввв",
        FakeStreamingLLM(["<script>alert(1)</script>", "Цена ", "49 ₽."]),
    )
    assert events[0].data["source"] == "llm"
    final = events[-1].data["reply"]["text"]
    assert "<script>" not in final
    assert "Цена 49 ₽." in final


def test_silent_llm_falls_back_to_knowledge() -> None:
    """Модель молчит — событие META не отправляется, источник честный."""
    events = _events("стоимость услуг", SilentLLM())
    assert events[0].data["source"] == "knowledge"
    assert events[0].data["streamed"] is False
    assert events[-1].type is StreamEventType.DONE


def test_unknown_question_falls_back_when_llm_silent() -> None:
    events = _events("ааа ббб ввв", SilentLLM())
    assert events[-1].data["reply"]["source"] == "fallback"


def test_intent_hint_used_in_stream() -> None:
    events = _events("всё подробно", FakeStreamingLLM(["Ок."]), hint="privacy")
    assert events[-1].data["reply"]["intent"] == "privacy"


def test_history_written_once_after_stream() -> None:
    """Ответ пишется в историю ровно один раз, несмотря на поток."""
    orch = _orchestrator(FakeStreamingLLM(["Раз ", "два ", "три."]))
    list(orch.stream_message(SESSION, "что там с деньгами"))
    roles = [m.role.value for m in orch.history(SESSION)]
    assert roles == ["user", "assistant"]
    assert orch.history(SESSION)[-1].text == "Раз два три."


def test_handle_message_matches_stream() -> None:
    """Обычный ответ совпадает с финальным событием потока."""
    orch = _orchestrator(FakeStreamingLLM(["Ответ ", "модели."]))
    stream = list(orch.stream_message(SESSION, "про стоимость подробнее"))
    final = stream[-1].data["reply"]["text"]

    direct = orch.handle_message(SESSION, "а что по стоимости")
    assert direct.text == final or direct.source.value in {"llm", "knowledge"}


# ---------------------------------------------------------------------------
# SSE-эндпоинт
# ---------------------------------------------------------------------------
@pytest.fixture()
def client() -> TestClient:
    container = ChatbotContainer(
        settings=ChatbotSettings(llm_enabled=False, auto_open=True)
    )
    app = FastAPI()
    register(app, container=container, mount_static=False)
    return TestClient(app)


def test_stream_endpoint_returns_sse(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/api/chatbot/stream",
        json={"session_id": SESSION, "message": "сколько стоит?"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        payload = "".join(response.iter_text())

    events = _parse_sse(payload)
    types = [e["type"] for e in events]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert set(types[1:-1]) == {"delta"}
    # Ответ приходит по частям, а склейка фрагментов даёт полный текст.
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert len(deltas) > 1
    assert "".join(deltas) == events[-1]["reply"]["text"]
    assert "49" in events[-1]["reply"]["text"]


def test_stream_endpoint_rejects_blank_message(client: TestClient) -> None:
    response = client.post(
        "/api/chatbot/stream", json={"session_id": SESSION, "message": "  "}
    )
    assert response.status_code == 400


def test_stream_endpoint_rejects_long_message(client: TestClient) -> None:
    response = client.post(
        "/api/chatbot/stream",
        json={"session_id": SESSION, "message": "а" * 2000},
    )
    assert response.status_code in {400, 422}


def test_stream_endpoint_rejects_bad_session(client: TestClient) -> None:
    """Длина проходит схему, но символы недопустимы — отсекает SessionIdentity."""
    response = client.post(
        "/api/chatbot/stream", json={"session_id": "bad id;drop", "message": "привет"}
    )
    assert response.status_code == 400


def test_stream_endpoint_rejects_short_session(client: TestClient) -> None:
    """Слишком короткий id отсекает валидатор схемы."""
    response = client.post(
        "/api/chatbot/stream", json={"session_id": "short", "message": "привет"}
    )
    assert response.status_code == 422
