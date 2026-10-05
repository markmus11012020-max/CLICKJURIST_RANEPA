"""Определение намерения посетителя по тексту сообщения."""
from __future__ import annotations

import re
from dataclasses import dataclass

from chatbot.domain.enums import Intent
from chatbot.domain.interfaces import KnowledgeBase
from chatbot.knowledge.base import normalize

#: Фразы, однозначно задающие намерение без анализа остального текста.
#: Проверяются до обращения к базе знаний: они короче и точнее.
EXACT_PHRASES: dict[str, Intent] = {
    "привет": Intent.GREETING,
    "здравствуйте": Intent.GREETING,
    "здравствуй": Intent.GREETING,
    "добрый день": Intent.GREETING,
    "доброе утро": Intent.GREETING,
    "добрый вечер": Intent.GREETING,
    "хай": Intent.GREETING,
    "приветик": Intent.GREETING,
    "спасибо": Intent.THANKS,
    "спасибо большое": Intent.THANKS,
    "благодарю": Intent.THANKS,
    "пока": Intent.GREETING,
    "до свидания": Intent.GREETING,
}

#: Регулярные выражения для личных/не-тематических вопросов. Ловятся ДО
#: основного ключевого сопоставления, чтобы «сколько мне лет?» не уходило
#: в статью про тарифы из-за жадного совпадения по слову «сколько».
#: Каждой категории соответствует свой ответ в ``orchestrator._SMALLTALK_RESPONSES``.
_SMALLTALK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Возраст / возраст пользователя / возраст бота.
    (
        "age",
        re.compile(
            r"\b(сколько\s+(тебе|мне|ему|ей)\s+лет|сколько\s+(тебе|мне|ему|ей)|"
            r"тво(й|ё)\s+возраст|како(й|е)\s+(ты|вы)\s+(возраст|года?)|"
            r"мне\s+\d+\s+лет)\b",
            re.IGNORECASE,
        ),
    ),
    # Имя бота / просьба представиться.
    (
        "name",
        re.compile(
            r"\b(как\s+тебя\s+(зовут|звать|имя)|"
            r"тво(ё|е)\s+им(я|ени)|"
            r"представ(ься|ись)|"
            r"как\s+тво(ё|е)\s+им(я|ени))\b",
            re.IGNORECASE,
        ),
    ),
    # «Кто ты?» / «Что ты?».
    (
        "who",
        re.compile(
            r"^\s*(ты\s+кто|кто\s+ты|ты\s+что|что\s+ты|что\s+ты\s+умеешь)\s*[\?\.]?\s*$",
            re.IGNORECASE,
        ),
    ),
    # «Ты человек / бот / робот / ИИ / настоящий?»
    (
        "human",
        re.compile(
            r"\b(ты\s+(человек|бот|робот|машина|нейросет|ии|живой|настоящ(ий|ая)|разумн))\b",
            re.IGNORECASE,
        ),
    ),
    # «Как дела / как жизнь / как сам».
    (
        "feelings",
        re.compile(
            r"\b(как\s+(дела|жизнь|сам|сама)|как\s+ты\s+(сам|сама|поживаешь)|"
            r"что\s+нового|как\s+настроени)\b",
            re.IGNORECASE,
        ),
    ),
    # Разное: проверка связи, «ау», «тест».
    (
        "ping",
        re.compile(
            r"^\s*(ау+|тест|проверк[аи]|алло|эй)\s*[\?\.!]*\s*$",
            re.IGNORECASE,
        ),
    ),
)

#: Маркеры, усиливающие вес найденного намерения.
_HINT_MARKERS: dict[Intent, tuple[str, ...]] = {
    Intent.PAYMENT: ("оплат", "картой", "сбп", "робокасса", "чек", "платёж"),
    Intent.PRIVACY: ("данные", "персональн", "аноним", "конфиденц", "152", "пдн"),
    Intent.DOCUMENT: ("иск", "документ", "заявлен", "претенз", "жалоб", "бланк"),
    Intent.CHECKLIST: ("чек-лист", "чек лист", "план действий", "список дел"),
    Intent.PRICING: ("цена", "цены", "стоимост", "тариф", "прайс", "сколько стоит"),
}

#: Слова, при которых текущий вопрос НЕ считается пустым по теме.
_MEANINGFUL_TOKENS = 2


@dataclass(frozen=True, slots=True)
class IntentMatch:
    """Результат разбора одного сообщения."""

    intent: Intent
    score: float
    """Уверенность 0.0–1.0. Используется для выбора между БЗ и LLM."""

    matched_keywords: tuple[str, ...] = ()

    @property
    def is_confident(self) -> bool:
        """Уверенность достаточна, чтобы ответить из базы знаний."""
        return self.score >= 0.45


class IntentResolver:
    """Правиловый классификатор намерений.

    Работает без сети и без модели: это дешёвый первый рубеж, который
    закрывает ~80% типовых вопросов о сервисе. Если уверенности не хватает,
    оркестратор передаёт вопрос языковой модели.
    """

    def __init__(self, knowledge: KnowledgeBase) -> None:
        self._knowledge = knowledge

    def resolve(self, text: str, *, hint: str | None = None) -> IntentMatch:
        """Определить намерение по тексту сообщения.

        Args:
            text: сообщение посетителя.
            hint: код быстрого ответа (кнопки под приветствием). Имеет
                приоритет над разбором текста — пользователь уже выбрал
                тему осознанно.
        """
        if hint:
            hinted = Intent(hint) if self._is_valid_intent(hint) else None
            if hinted is not None:
                return IntentMatch(intent=hinted, score=1.0)

        normalized = normalize(text)
        if not normalized:
            return IntentMatch(intent=Intent.FALLBACK, score=0.0)

        exact = self._match_exact(normalized)
        if exact is not None:
            return exact

        # Личные/не-тематические вопросы — раньше ключевого сопоставления,
        # чтобы «сколько мне лет?» не уходило в тарифы из-за жадного
        # совпадения по слову «сколько».
        smalltalk = self._match_smalltalk(normalized)
        if smalltalk is not None:
            return smalltalk

        return self._match_by_keywords(normalized)

    # -- Внутреннее -----------------------------------------------------------
    @staticmethod
    def _is_valid_intent(value: str) -> bool:
        try:
            Intent(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _match_exact(normalized: str) -> IntentMatch | None:
        """Совпадение с короткой фразой (с учётом знаков препинания)."""
        stripped = normalized.strip(" .,!?…-«»\"'")
        intent = EXACT_PHRASES.get(stripped)
        if intent is not None:
            return IntentMatch(intent=intent, score=1.0, matched_keywords=(stripped,))
        for phrase, candidate in EXACT_PHRASES.items():
            if stripped.startswith(phrase) and len(stripped) <= len(phrase) + 12:
                return IntentMatch(
                    intent=candidate, score=0.9, matched_keywords=(phrase,)
                )
        return None

    @staticmethod
    def _match_smalltalk(normalized: str) -> IntentMatch | None:
        """Распознать личные/тролль-вопросы и сгруппировать по теме.

        Возвращает ``IntentMatch(intent=SMALLTALK, score=1.0, matched=...)``
        с ключом категории в ``matched_keywords[0]``. Оркестратор использует
        этот ключ, чтобы выбрать подходящий остроумный ответ.
        """
        for key, pattern in _SMALLTALK_PATTERNS:
            if pattern.search(normalized):
                return IntentMatch(
                    intent=Intent.SMALLTALK,
                    score=1.0,
                    matched_keywords=(key,),
                )
        return None

    def _match_by_keywords(self, normalized: str) -> IntentMatch:
        """Подбор по ключевым словам базы знаний и маркерам."""
        if not hasattr(self._knowledge, "match_intents"):
            # Контракт KnowledgeBase не обещает поиск по ключевым словам —
            # деградируем до FALLBACK, а не падаем.
            return IntentMatch(intent=Intent.FALLBACK, score=0.0)

        matches = self._knowledge.match_intents(normalized)  # type: ignore[attr-defined]
        if not matches:
            return IntentMatch(intent=Intent.FALLBACK, score=0.0)

        intent, hits = matches[0]
        # Метки усиливают уверенность, если совпали с тем же намерением.
        bonus = sum(
            1
            for marker in _HINT_MARKERS.get(intent, ())
            if marker in normalized
        )
        tokens = normalized.split()
        meaningful = 1.0 if len(tokens) >= _MEANINGFUL_TOKENS else 0.75
        # Одно точное совпадение ключевого слова («документы», «оплатить»)
        # уже достаточно, чтобы ответить из базы знаний: она авторитетна
        # по теме сервиса. Два независимых совпадения — максимум.
        raw = (hits + bonus * 0.5) / 1.5
        score = max(0.0, min(1.0, raw * meaningful))

        matched = tuple(
            word
            for word in self._collect_keywords(intent)
            if word in normalized
        )[:4]
        return IntentMatch(intent=intent, score=score, matched_keywords=matched)

    def _collect_keywords(self, intent: Intent) -> tuple[str, ...]:
        words: list[str] = []
        for article in self._knowledge.find(intent):
            words.extend(normalize(k) for k in article.keywords)
        return tuple(words)
