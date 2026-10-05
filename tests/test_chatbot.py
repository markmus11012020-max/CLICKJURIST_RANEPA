"""Тесты доменного слоя чат-бота: интенты, приветствие, знания, санитайзер.

Покрывают стартовый сценарий (приветствие) и деградацию при
недоступной языковой модели — обе ситуации должны работать без сети.
"""

from __future__ import annotations

import pytest

from chatbot.domain.enums import AnswerSource, Intent
from chatbot.exceptions import EmptyMessageError, MessageTooLongError
from chatbot.knowledge import catalog
from chatbot.knowledge.base import StaticKnowledgeBase
from chatbot.llm.gateway import NullLLMGateway
from chatbot.repositories.in_memory import InMemoryConversationRepository
from chatbot.services.greeting import GreetingService
from chatbot.services.intent_resolver import IntentResolver
from chatbot.services.orchestrator import ChatOrchestrator
from chatbot.services.response_sanitizer import ResponseSanitizer


@pytest.fixture()
def knowledge() -> StaticKnowledgeBase:
    return StaticKnowledgeBase()


@pytest.fixture()
def orchestrator() -> ChatOrchestrator:
    """Оркестратор без LLM: весь ответ — из базы знаний."""
    kb = StaticKnowledgeBase()
    return ChatOrchestrator(
        repository=InMemoryConversationRepository(max_conversations=10),
        knowledge=kb,
        llm=NullLLMGateway(),
        greeting=GreetingService(),
        max_message_length=200,
    )


# ---------------------------------------------------------------------------
# Приветствие
# ---------------------------------------------------------------------------
def test_greeting_first_visit_mentions_brand(orchestrator: ChatOrchestrator) -> None:
    greeting = orchestrator.greet("session-0001")
    assert "КликЮрист" in greeting.text
    assert greeting.is_returning is False
    assert len(greeting.quick_replies) >= 3


def test_greeting_offers_pricing_and_privacy(orchestrator: ChatOrchestrator) -> None:
    greeting = orchestrator.greet("session-0002")
    hints = {qr.intent_hint for qr in greeting.quick_replies}
    assert Intent.PRICING.value in hints
    assert Intent.PRIVACY.value in hints


def test_greeting_recorded_in_history_once(orchestrator: ChatOrchestrator) -> None:
    orchestrator.greet("session-0003")
    orchestrator.greet("session-0003")
    assert len(orchestrator.history("session-0003")) == 1


def test_greeting_becomes_returning_after_reply(orchestrator: ChatOrchestrator) -> None:
    orchestrator.greet("session-0004")
    orchestrator.handle_message("session-0004", "сколько стоит?")
    greeting = orchestrator.greet("session-0004")
    assert greeting.is_returning is True


# ---------------------------------------------------------------------------
# Определение намерения
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("привет", Intent.GREETING),
        ("Спасибо большое!", Intent.THANKS),
        ("сколько стоит консультация", Intent.PRICING),
        ("как оплатить картой", Intent.PAYMENT),
        ("что с моими данными", Intent.PRIVACY),
        ("какие есть документы", Intent.DOCUMENT),
        ("как это работает", Intent.STEPS),
    ],
)
def test_resolver_detects_intent(
    knowledge: StaticKnowledgeBase, text: str, expected: Intent
) -> None:
    match = IntentResolver(knowledge).resolve(text)
    assert match.intent is expected


def test_resolver_unknown_text_is_fallback(knowledge: StaticKnowledgeBase) -> None:
    match = IntentResolver(knowledge).resolve("ааа ббб ввв")
    assert match.intent is Intent.FALLBACK
    assert match.is_confident is False


def test_resolver_hint_has_priority(knowledge: StaticKnowledgeBase) -> None:
    """Клик по кнопке важнее текста — он уже выбрал тему осознанно."""
    match = IntentResolver(knowledge).resolve("привет", hint="pricing")
    assert match.intent is Intent.PRICING
    assert match.score == 1.0


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------
def test_pricing_answer_contains_all_prices(orchestrator: ChatOrchestrator) -> None:
    reply = orchestrator.handle_message("session-0010", "сколько стоит?")
    assert "49" in reply.text and "50" in reply.text and "150" in reply.text
    assert reply.source is AnswerSource.KNOWLEDGE
    assert reply.intent == Intent.PRICING.value


def test_privacy_answer_mentions_law(orchestrator: ChatOrchestrator) -> None:
    reply = orchestrator.handle_message("session-0011", "что с моими данными?")
    assert "152-ФЗ" in reply.text
    assert "Zero-Storage" in reply.text


def test_thanks_reply_is_short(orchestrator: ChatOrchestrator) -> None:
    reply = orchestrator.handle_message("session-0012", "спасибо!")
    assert reply.source is AnswerSource.SCRIPTED
    assert len(reply.text) < 120


def test_unknown_question_falls_back(orchestrator: ChatOrchestrator) -> None:
    """Без LLM неизвестный вопрос не должен приводить к пустому ответу."""
    reply = orchestrator.handle_message("session-0013", "ааа ббб ввв")
    assert reply.text.strip()
    assert reply.source is AnswerSource.FALLBACK
    assert len(reply.quick_replies) >= 2


def test_reply_after_turn_offers_follow_up(orchestrator: ChatOrchestrator) -> None:
    reply = orchestrator.handle_message("session-0014", "сколько стоит?")
    assert reply.quick_replies


def test_quick_reply_hint_reaches_intent(orchestrator: ChatOrchestrator) -> None:
    """Ответ по кнопке «Что с моими данными?» — про 152-ФЗ, а не про цену."""
    reply = orchestrator.handle_message("session-0015", "всё подробно", intent_hint="privacy")
    assert reply.intent == Intent.PRIVACY.value


# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------
def test_empty_message_rejected(orchestrator: ChatOrchestrator) -> None:
    with pytest.raises(EmptyMessageError):
        orchestrator.handle_message("session-0016", "   ")


def test_too_long_message_rejected(orchestrator: ChatOrchestrator) -> None:
    with pytest.raises(MessageTooLongError):
        orchestrator.handle_message("session-0017", "а" * 500)


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------
def test_history_accumulates(orchestrator: ChatOrchestrator) -> None:
    orchestrator.greet("session-0018")
    orchestrator.handle_message("session-0018", "сколько стоит?")
    history = orchestrator.history("session-0018")
    roles = [m.role.value for m in history]
    assert roles.count("assistant") == 2
    assert roles.count("user") == 1


def test_sessions_are_isolated(orchestrator: ChatOrchestrator) -> None:
    orchestrator.handle_message("session-0019", "сколько стоит?")
    assert orchestrator.history("session-0020") == []


def test_repository_resets_session(orchestrator: ChatOrchestrator) -> None:
    orchestrator.handle_message("session-0021", "сколько стоит?")
    orchestrator.reset("session-0021")
    assert orchestrator.history("session-0021") == []


# ---------------------------------------------------------------------------
# Санитайзер
# ---------------------------------------------------------------------------
def test_sanitizer_strips_html() -> None:
    cleaned = ResponseSanitizer().clean("Привет <script>alert(1)</script> мир")
    assert "<script>" not in cleaned
    assert "Привет" in cleaned


def test_sanitizer_removes_think_blocks() -> None:
    cleaned = ResponseSanitizer().clean("<think>секрет</think>Ответ")
    assert "секрет" not in cleaned
    assert cleaned == "Ответ"


def test_sanitizer_limits_length() -> None:
    result = ResponseSanitizer(max_length=50).process("а" * 500)
    assert len(result) <= 51
    assert result.endswith("…")


# ---------------------------------------------------------------------------
# База знаний
# ---------------------------------------------------------------------------
def test_every_intent_has_article(knowledge: StaticKnowledgeBase) -> None:
    """Каждая кнопка быстрого ответа ведёт в существующую статью."""
    for _, _, hint in catalog.GREETING_QUICK_REPLIES + catalog.RETURNING_QUICK_REPLIES:
        assert knowledge.has_article(Intent(hint)), hint


def test_catalog_prices_match_site(knowledge: StaticKnowledgeBase) -> None:
    """Цены в базе знаний совпадают с каталогом шагов."""
    pricing = knowledge.render(Intent.PRICING)
    for step in catalog.SERVICE_STEPS:
        assert f"{step.price}" in pricing
