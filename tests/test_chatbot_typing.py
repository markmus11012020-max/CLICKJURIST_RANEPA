"""Регрессионные тесты потоковой выдачи готовых ответов.

Пользователь жаловался, что ответы приходят «мёртвым серым полем» без
стриминга. Причина была в ``_emit_ready``: текст из базы знаний уходил
одним фрагментом, поэтому интерфейс не мог набирать его постепенно.

Ключевой инвариант: ``"".join(фрагменты)`` обязан давать исходный текст
символ в символ. Раньше разделители терялись на границах фрагментов, и слова
слипались — визуально это выглядело как «сломанный» ответ.
"""
from __future__ import annotations

import pytest

from chatbot.domain.entities import BotReply
from chatbot.domain.enums import AnswerSource
from chatbot.knowledge.base import StaticKnowledgeBase
from chatbot.llm.gateway import NullLLMGateway
from chatbot.repositories.in_memory import InMemoryConversationRepository
from chatbot.services.orchestrator import ChatOrchestrator


@pytest.fixture()
def orchestrator() -> ChatOrchestrator:
    """Оркестратор без LLM: весь ответ — из базы знаний.

    Пауза между фрагментами нулевая, чтобы тесты не ждали реальную печать.
    """
    return ChatOrchestrator(
        repository=InMemoryConversationRepository(max_conversations=20),
        knowledge=StaticKnowledgeBase(),
        llm=NullLLMGateway(),
        type_chunk_size=18,
        type_delay_s=0.0,
    )


def _deltas(orchestrator: ChatOrchestrator, session: str, message: str) -> tuple[list[str], str]:
    """Прогнать сообщение и вернуть (фрагменты, финальный текст)."""
    from chatbot.llm.streaming import StreamEventType

    deltas: list[str] = []
    final = ""
    for event in orchestrator.stream_message(session, message):
        if event.type is StreamEventType.DELTA:
            deltas.append(str(event.data.get("text", "")))
        elif event.type is StreamEventType.DONE:
            final = str(event.data["reply"].get("text", ""))
    return deltas, final


def test_knowledge_answer_is_split_into_several_pieces(orchestrator: ChatOrchestrator):
    """Ответ из базы знаний приходит несколькими фрагментами, а не одним."""
    deltas, _ = _deltas(orchestrator, "stream-split-1", "Сколько стоит сервис")

    assert len(deltas) > 1, "готовый ответ должен дробиться, иначе нет стриминга"
    assert max(len(piece) for piece in deltas) < len("".join(deltas)) / 2, (
        "фрагменты получились неоправданно крупными"
    )


def test_fragments_reassemble_into_identical_text(orchestrator: ChatOrchestrator):
    """Склейка фрагментов совпадает с финальным текстом символ в символ."""
    deltas, final = _deltas(orchestrator, "stream-exact-1", "Сколько стоит сервис")

    assert final, "финальный ответ не должен быть пустым"
    assert "".join(deltas) == final, (
        "разделители теряются на границах фрагментов — текст не восстановится"
    )


def test_words_are_not_split_mid_word(orchestrator: ChatOrchestrator):
    """Ни один фрагмент не обрывает слово на середине."""
    deltas, final = _deltas(orchestrator, "stream-words-1", "Как это работает?")

    for piece in deltas:
        # Если фрагмент не начинается с разделителя, он обязан начинаться
        # с начала слова: нечётная позиция старта означала бы разрыв.
        offset = final.find(piece)
        assert offset >= 0, f"фрагмент не найден в исходном тексте: {piece!r}"
        if offset:
            assert piece[0].isspace() or final[offset - 1].isspace(), (
                f"слово разорвано на границе фрагментов: {piece[:20]!r}"
            )


@pytest.mark.parametrize(
    "message",
    [
        "Сколько стоит сервис",
        "Как это работает?",
        "Что с моими данными?",
        "Как оплатить?",
        "абракадабра",
    ],
)
def test_every_answer_survives_fragmentation(
    orchestrator: ChatOrchestrator, message: str
):
    """Ни один сценарий не теряет текст при нарезке на фрагменты."""
    deltas, final = _deltas(orchestrator, f"stream-params-{abs(hash(message))}", message)

    assert final
    assert len(deltas) >= 1
    assert "".join(deltas) == final


def test_plain_reply_matches_streamed_text(orchestrator: ChatOrchestrator):
    """Обычный ответ и поток дают одинаковый текст.

    Иначе один и тот же вопрос получал бы разные формулировки в зависимости
    от того, открыт ли виджет в браузере.
    """
    streamed_deltas, streamed_text = _deltas(
        orchestrator, "stream-parity-1", "Сколько стоит сервис"
    )
    plain = orchestrator.handle_message("stream-parity-2", "Сколько стоит сервис")

    assert "".join(streamed_deltas) == streamed_text
    assert streamed_text == plain.text
    assert plain.source is AnswerSource.KNOWLEDGE


def test_meta_reports_unstreamed_source(orchestrator: ChatOrchestrator):
    """Ответ из базы знаний честно помечен как не сгенерированный моделью."""
    from chatbot.llm.streaming import StreamEventType

    for event in orchestrator.stream_message("stream-meta-1", "Сколько стоит сервис"):
        if event.type is StreamEventType.META:
            assert event.data["source"] == AnswerSource.KNOWLEDGE.value
            assert event.data["streamed"] is False
            return
    pytest.fail("событие META не отправлено")


def test_empty_text_is_rejected_before_streaming(orchestrator: ChatOrchestrator):
    """Пустая реплика отвергается до потока, а не превращается в поток пробелов."""
    from chatbot.exceptions import EmptyMessageError

    with pytest.raises(EmptyMessageError):
        _deltas(orchestrator, "stream-empty-1", "   ")


def test_short_reply_not_broken_into_pieces(orchestrator: ChatOrchestrator):
    """Короткий ответ не дробится на буквы — достаточно одного фрагмента."""
    reply = BotReply(
        text="Да.",
        quick_replies=(),
        intent="thanks",
        source=AnswerSource.SCRIPTED,
    )
    pieces = list(orchestrator._split_for_typing(reply.text))
    assert pieces == ["Да."]
