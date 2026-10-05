"""Оркестратор диалога — единственная точка orchestration модуля.

Сценарий обработки сообщения:

    валидация → история → намерение → сценарий / база знаний → (LLM) → санитайзер

Единая точка входа — :meth:`ChatOrchestrator.stream_message`, генератор
событий ``META → DELTA* → DONE``. Обычный ответ
(:meth:`ChatOrchestrator.handle_message`) собирает тот же поток целиком,
поэтому решения об источнике ответа не могут разойтись между путями.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator

from chatbot.domain.entities import BotReply, ChatMessage, Conversation, QuickReply
from chatbot.domain.enums import AnswerSource, Intent, MessageRole
from chatbot.domain.interfaces import (
    ConversationRepository,
    KnowledgeBase,
    LLMGateway,
)
from chatbot.exceptions import EmptyMessageError, MessageTooLongError
from chatbot.knowledge import catalog
from chatbot.llm.streaming import StreamEvent, StreamEventType
from chatbot.services.greeting import Greeting, GreetingService
from chatbot.services.intent_resolver import IntentResolver
from chatbot.services.response_sanitizer import ResponseSanitizer

logger = logging.getLogger(__name__)

#: Быстрые ответы, предлагаемые, когда бот не понял вопрос.
_FALLBACK_QUICK_REPLIES: tuple[QuickReply, ...] = (
    QuickReply(id="pricing", label="Сколько стоит?", intent_hint=Intent.PRICING.value),
    QuickReply(id="steps", label="Как это работает?", intent_hint=Intent.STEPS.value),
    QuickReply(id="privacy", label="Что с моими данными?", intent_hint=Intent.PRIVACY.value),
)

#: Ответ на «спасибо» — короткий, чтобы не жечь токены.
_THANKS_TEXT = "Всегда рад помочь! Если появятся вопросы по сервису — пишите."

#: Остроумные ответы на личные/тролль-вопросы. Ключ — категория из
#: ``_SMALLTALK_PATTERNS`` в ``intent_resolver``. Тон: дружелюбно, без
#: формальностей, всегда с мягким возвратом к теме сервиса.
_SMALLTALK_RESPONSES: dict[str, str] = {
    "age": (
        "Я всегда только что родился — у ботов возраст измеряется в секундах от запуска. "
        "А ваш возраст, кстати, сервису знать не нужно: для ответа это не важно. "
        "Может, расскажете лучше о своей ситуации?"
    ),
    "name": (
        "Зовите меня КликЮрист — я ваш помощник по правовым вопросам. "
        "А как зовут вас, я и не спрашиваю: имя нам для ответа не нужно, "
        "и в базе оно не сохраняется."
    ),
    "who": (
        "Я — КликЮрист, цифровой помощник по правовым задачам. "
        "Умею разбирать бытовые и бизнес-споры, составлять чек-листы и готовить "
        "документы. Чем могу помочь?"
    ),
    "human": (
        "Нет, я бот — и этим горжусь. Не устаю, не забываю, не сужу. "
        "А если нужен живой юрист — команда поддержки подскажет, куда обратиться."
    ),
    "feelings": (
        "У меня всё отлично — я же работаю без выходных. "
        "А у вас как? Если что-то тревожит по правовому вопросу — расскажите, разберёмся."
    ),
    "ping": (
        "Я здесь 👋 Если есть вопрос по сервису — задавайте, я с радостью отвечу."
    ),
}

#: Быстрые ответы после small-talk — мягко возвращаем к теме сервиса.
_SMALLTALK_FOLLOW_UPS: tuple[QuickReply, ...] = (
    QuickReply(id="steps", label="Как это работает?", intent_hint=Intent.STEPS.value),
    QuickReply(id="pricing", label="Сколько стоит?", intent_hint=Intent.PRICING.value),
    QuickReply(id="privacy", label="Что с моими данными?", intent_hint=Intent.PRIVACY.value),
)

#: Локальная копия паттернов: позволяет оркестратору без зависимости от
#: ``intent_resolver`` перевести текст в ключ категории и подобрать
#: подходящий ответ из ``_SMALLTALK_RESPONSES``.
_SMALLTALK_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("age", re.compile(
        r"\b(сколько\s+(тебе|мне|ему|ей)\s+лет|тво(й|ё)\s+возраст|"
        r"како(й|е)\s+(ты|вы)\s+(возраст|года?)|мне\s+\d+\s+лет)\b",
        re.IGNORECASE,
    )),
    ("name", re.compile(
        r"\b(как\s+тебя\s+(зовут|звать|имя)|"
        r"тво(ё|е)\s+им(я|ени)|представ(ься|ись))\b",
        re.IGNORECASE,
    )),
    ("who", re.compile(
        r"^\s*(ты\s+кто|кто\s+ты|ты\s+что|что\s+ты)\s*[\?\.]?\s*$",
        re.IGNORECASE,
    )),
    ("human", re.compile(
        r"\b(ты\s+(человек|бот|робот|машина|нейросет|ии|живой|настоящ(ий|ая)))\b",
        re.IGNORECASE,
    )),
    ("feelings", re.compile(
        r"\b(как\s+(дела|жизнь|сам|сама)|что\s+нового)\b",
        re.IGNORECASE,
    )),
    ("ping", re.compile(
        r"^\s*(ау+|тест|проверк[аи]|алло|эй)\s*[\?\.!]*\s*$",
        re.IGNORECASE,
    )),
)


def _classify_smalltalk(text: str) -> str:
    """Подобрать ключ категории small talk по тексту."""
    for key, pattern in _SMALLTALK_KEY_PATTERNS:
        if pattern.search(text):
            return key
    return "ping"


def _smalltalk_reply(text: str) -> BotReply:
    """Собрать остроумный ответ на личный/тролль-вопрос."""
    key = _classify_smalltalk(text)
    body = _SMALLTALK_RESPONSES.get(key) or _SMALLTALK_RESPONSES["ping"]
    return BotReply(
        text=body,
        quick_replies=_SMALLTALK_FOLLOW_UPS,
        intent=Intent.SMALLTALK.value,
        source=AnswerSource.SCRIPTED,
    )


class ChatOrchestrator:
    """Сценарии использования чат-бота.

    Единственный класс, который знает обо всех сервисах. API-слой и
    виджет общаются только с ним.
    """

    def __init__(
        self,
        repository: ConversationRepository,
        knowledge: KnowledgeBase,
        llm: LLMGateway,
        greeting: GreetingService | None = None,
        resolver: IntentResolver | None = None,
        sanitizer: ResponseSanitizer | None = None,
        *,
        max_message_length: int = 1000,
        max_history_messages: int = 10,
        rate_limit_per_minute: int = 20,
        type_chunk_size: int = 18,
        type_delay_s: float = 0.02,
    ) -> None:
        self._repository = repository
        self._knowledge = knowledge
        self._llm = llm
        self._greeting = greeting or GreetingService()
        self._resolver = resolver or IntentResolver(knowledge)
        self._sanitizer = sanitizer or ResponseSanitizer()
        self._max_message_length = max_message_length
        self._max_history = max_history_messages
        self._rate_limit = rate_limit_per_minute
        # Размер порции и пауза для «живой печати» готовых ответов.
        self._type_chunk = type_chunk_size
        self._type_delay = type_delay_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._hits_lock = threading.Lock()

    # -- Сценарий 1: приветствие ---------------------------------------------
    def greet(self, session_id: str) -> Greeting:
        """Собрать приветствие для посетителя.

        Первый вызов в сессии сразу записывает реплику бота в историю,
        чтобы контекст для модели был непустым уже со второго сообщения.
        """
        conversation = self._repository.get_or_create(session_id)
        greeting = self._greeting.build(turn_count=conversation.turn_count)

        already_greeted = any(
            m.role is MessageRole.ASSISTANT for m in conversation.messages
        )
        if not already_greeted:
            conversation.add_message(
                ChatMessage(role=MessageRole.ASSISTANT, text=greeting.text)
            )
            self._repository.save(conversation)
        return greeting

    # -- Сценарий 2: ответ на сообщение --------------------------------------
    def stream_message(
        self,
        session_id: str,
        text: str,
        *,
        intent_hint: str | None = None,
    ) -> Iterator[StreamEvent]:
        """Обработать реплику, отдавая ответ по частям.

        Поток событий: ``META`` → ``DELTA``* → ``DONE``.
        Текст ответа пишется в историю ровно один раз — в ``DONE``.

        Raises:
            EmptyMessageError: пустое сообщение.
            MessageTooLongError: превышена длина сообщения.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            raise EmptyMessageError("Пустое сообщение")
        if len(cleaned) > self._max_message_length:
            raise MessageTooLongError(self._max_message_length)

        self._check_rate_limit(session_id)

        conversation = self._repository.get_or_create(session_id)
        conversation.add_message(ChatMessage(role=MessageRole.USER, text=cleaned))
        match = self._resolver.resolve(cleaned, hint=intent_hint)
        intent = match.intent

        # 1. Сценарии, которые не требуют ни базы знаний, ни модели.
        scripted = self._scripted_reply(intent, conversation, text=cleaned)
        if scripted is not None:
            yield from self._emit_ready(scripted, conversation)
            return

        # 2. Уверенный матч и есть статья → мгновенный ответ без модели.
        if match.is_confident and self._has_article(intent):
            yield from self._emit_ready(self._knowledge_reply(intent), conversation)
            return

        # 3. Потоковая генерация моделью.
        if self._llm.is_available():
            streamed = yield from self._stream_with_llm(
                cleaned, intent, conversation
            )
            if streamed:
                return

        # 4. Модель недоступна или не ответила — заготовка из базы знаний.
        yield from self._emit_ready(self._fallback(intent), conversation)

    def handle_message(
        self,
        session_id: str,
        text: str,
        *,
        intent_hint: str | None = None,
    ) -> BotReply:
        """Обработать реплику и вернуть готовый ответ бота.

        Обёртка над :meth:`stream_message` для клиентов без поддержки SSE.
        """
        reply: BotReply | None = None
        for event in self.stream_message(session_id, text, intent_hint=intent_hint):
            if event.type is StreamEventType.DONE:
                reply = BotReply(
                    text=str(event.data["reply"].get("text", "")),
                    quick_replies=tuple(
                        QuickReply(
                            id=qr.get("id", ""),
                            label=qr.get("label", ""),
                            intent_hint=qr.get("intent_hint"),
                        )
                        for qr in event.data["reply"].get("quick_replies", [])
                    ),
                    intent=str(event.data["reply"].get("intent", "fallback")),
                    source=_source_from(str(event.data["reply"].get("source", "fallback"))),
                )
        if reply is None:  # pragma: no cover — защита от «пустого» потока
            reply = self._fallback(Intent.FALLBACK)
        return reply

    # -- Служебные сценарии ----------------------------------------------------
    def history(self, session_id: str, limit: int = 50) -> list[ChatMessage]:
        """История диалога для клиента."""
        return self._repository.history(session_id, limit)

    def reset(self, session_id: str) -> None:
        """Очистить диалог сессии (кнопка «Начать заново»)."""
        resetter = getattr(self._repository, "reset", None)
        if callable(resetter):
            resetter(session_id)
            return
        # У репозитория без reset() начинаем диалог заново, записав пустой.
        self._repository.save(self._repository.get_or_create(session_id))

    def conversation_count(self) -> int:
        """Количество живых диалогов (для /health)."""
        counter = getattr(self._repository, "count", None)
        return int(counter()) if callable(counter) else 0

    def turn_count(self, session_id: str) -> int:
        """Сколько реплик посетителя уже было в диалоге."""
        return self._repository.get_or_create(session_id).turn_count

    def llm_available(self) -> bool:
        """Готов ли LLM-шлюз отвечать (для /health)."""
        return self._llm.is_available()

    def knowledge_article_count(self) -> int:
        """Количество статей базы знаний."""
        return sum(len(self._knowledge.find(intent)) for intent in Intent)

    def llm_provider(self) -> str:
        """Имя последнего ответившего провайдера."""
        return self._llm.last_provider

    # -- Внутреннее: источники ответа -----------------------------------------
    def _scripted_reply(
        self, intent: Intent, conversation: Conversation, text: str = ""
    ) -> BotReply | None:
        """Ответ из детерминированного сценария, если он есть."""
        if intent is Intent.THANKS:
            return BotReply.scripted(_THANKS_TEXT, intent=Intent.THANKS.value)
        if intent is Intent.SMALLTALK:
            return _smalltalk_reply(text)
        if intent is Intent.GREETING:
            greeting = self._greeting.build(turn_count=conversation.turn_count)
            return BotReply(
                text=greeting.text,
                quick_replies=greeting.quick_replies,
                intent=Intent.GREETING.value,
                source=AnswerSource.SCRIPTED,
            )
        return None

    def _knowledge_reply(self, intent: Intent) -> BotReply:
        """Ответ из статьи базы знаний."""
        return BotReply(
            text=self._sanitizer.process(self._knowledge.render(intent)),
            quick_replies=self._follow_up_quick_replies(intent),
            intent=intent.value,
            source=AnswerSource.KNOWLEDGE,
        )

    def _fallback(self, intent: Intent) -> BotReply:
        """Заготовка на случай, когда модель недоступна."""
        if self._has_article(intent):
            return self._knowledge_reply(intent)
        return BotReply(
            text=self._sanitizer.process(catalog.FALLBACK_TEXT),
            quick_replies=_FALLBACK_QUICK_REPLIES,
            intent=Intent.FALLBACK.value,
            source=AnswerSource.FALLBACK,
        )

    # -- Внутреннее: поток ----------------------------------------------------
    def _emit_ready(
        self, reply: BotReply, conversation: Conversation
    ) -> Iterator[StreamEvent]:
        """Отдать готовый ответ как поток событий ``META`` → ``DELTA``* → ``DONE``.

        Ответ из базы знаний известен целиком, но отправлять его одним
        куском нельзя: в интерфейсе он выглядит как мёртвое серое поле вместо
        живой реплики. Поэтому текст режется на фрагменты по границам слов и
        отдаётся с небольшой паузой — так печать выглядит «набираемой».

        Клиенту не нужно знать, был ответ из базы знаний или из сценария:
        событие ``META`` в любом случае несёт метку ``streamed``.
        """
        self._commit(conversation, reply)
        yield StreamEvent.meta(
            intent=reply.intent, source=reply.source.value, streamed=False
        )

        for piece in self._split_for_typing(reply.text):
            yield StreamEvent.delta(piece)
            time.sleep(self._type_delay)

        yield StreamEvent.done(reply)

    def _split_for_typing(self, text: str) -> Iterator[str]:
        """Разбить текст на фрагменты, не разрывая слова.

        Разделители (пробел, перевод строки) приклеиваются к предыдущему
        фрагменту, поэтому склейка ``"".join(pieces)`` даёт исходный текст
        символ в символ. Если отдать разделитель в следующий фрагмент, пробел
        на границе теряется и слова в клиенте слипаются.
        """
        buffer = ""
        for token in re.findall(r"\s+|\S+", text):
            candidate = buffer + token
            if len(candidate) >= self._type_chunk and token.isspace():
                # Порция набрана, и текущий токен — разделитель: отдаём
                # накопленное вместе с разделителем, чтобы не потерять его.
                yield candidate
                buffer = ""
                continue
            buffer = candidate
        if buffer:
            yield buffer

    def _stream_with_llm(
        self, text: str, intent: Intent, conversation: Conversation
    ) -> Iterator[StreamEvent]:
        """Потоковый ответ модели. Возвращает ``True``, если ответ получен.

        Событие ``META`` отдаётся лениво — перед первым фрагментом. Если
        провайдер не отдал ни слова, событие не отправляется вовсе, и
        вызывающий код уходит в запасной путь с честной меткой источника.
        """
        context = self._knowledge.context_for_llm(intent)
        if not context and intent is Intent.FALLBACK:
            context = self._knowledge.context_for_llm(Intent.SERVICES)
        history = conversation.history_for_llm(self._max_history)[:-1]

        pieces = self._llm.stream_answer(text, context, history)
        if pieces is None:
            return False

        buffer: list[str] = []
        announced = False
        for piece in pieces:
            if not piece:
                continue
            if not announced:
                yield StreamEvent.meta(
                    intent=intent.value,
                    source=AnswerSource.LLM.value,
                    streamed=True,
                )
                announced = True
            buffer.append(piece)
            yield StreamEvent.delta(piece)

        answer = self._sanitizer.process("".join(buffer))
        if not answer:
            return False

        reply = BotReply(
            text=answer,
            quick_replies=self._follow_up_quick_replies(intent),
            intent=intent.value,
            source=AnswerSource.LLM,
        )
        conversation.last_intent = intent.value
        self._commit(conversation, reply)
        yield StreamEvent.done(reply)
        return True

    def _commit(self, conversation: Conversation, reply: BotReply) -> None:
        """Записать ответ бота в историю диалога."""
        conversation.add_message(
            ChatMessage(role=MessageRole.ASSISTANT, text=reply.text)
        )
        self._repository.save(conversation)

    # -- Внутреннее: вспомогательное ------------------------------------------
    def _has_article(self, intent: Intent) -> bool:
        """Есть ли в базе знаний статья по намерению."""
        checker = getattr(self._knowledge, "has_article", None)
        if callable(checker):
            return bool(checker(intent))
        return bool(self._knowledge.find(intent))

    @staticmethod
    def _follow_up_quick_replies(intent: Intent) -> tuple[QuickReply, ...]:
        """Быстрые ответы, уместные после ответа по теме."""
        if intent is Intent.PRICING:
            return (
                QuickReply(id="steps", label="Как это работает?", intent_hint=Intent.STEPS.value),
                QuickReply(id="payment", label="Как оплатить?", intent_hint=Intent.PAYMENT.value),
            )
        if intent is Intent.STEPS:
            return (
                QuickReply(id="document", label="Какие есть документы?", intent_hint=Intent.DOCUMENT.value),
                QuickReply(id="pricing", label="Сколько стоит?", intent_hint=Intent.PRICING.value),
            )
        if intent is Intent.DOCUMENT:
            return (
                QuickReply(id="checklist", label="А чек-лист?", intent_hint=Intent.CHECKLIST.value),
                QuickReply(id="pricing", label="Сколько стоит?", intent_hint=Intent.PRICING.value),
            )
        return _FALLBACK_QUICK_REPLIES

    def _check_rate_limit(self, session_id: str) -> None:
        """Простейший лимит частоты: N сообщений в минуту на сессию.

        Превышение не прерывает диалог ошибкой — посетитель получит
        заготовку, а не «HTTP 429» посреди разговора.
        """
        if self._rate_limit <= 0:
            return
        now = time.monotonic()
        with self._hits_lock:
            bucket = self._hits[session_id]
            while bucket and now - bucket[0] > 60.0:
                bucket.popleft()
            if len(bucket) >= self._rate_limit:
                logger.info("Rate limit чат-бота для сессии %s…", session_id[:12])
                return
            bucket.append(now)


def _source_from(value: str) -> AnswerSource:
    """Разобрать метку источника ответа, пришедшую по потоку."""
    try:
        return AnswerSource(value)
    except ValueError:
        return AnswerSource.FALLBACK
