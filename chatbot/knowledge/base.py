"""Реализация базы знаний о сервисе поверх :mod:`chatbot.knowledge.catalog`."""
from __future__ import annotations

import re

from chatbot.domain.enums import Intent
from chatbot.domain.interfaces import KnowledgeArticle, KnowledgeBase
from chatbot.knowledge import catalog

#: Нормализация текста перед поиском: единый регистр и «ё» → «е».
_FOLD_RE = re.compile(r"[ёЁ]")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Привести текст к виду, удобному для сопоставления по ключевым словам."""
    lowered = _FOLD_RE.sub("е", text.lower())
    return _WS_RE.sub(" ", lowered).strip()


class _ArticleAdapter(KnowledgeArticle):
    """Адаптер :class:`chatbot.knowledge.catalog.Article` под контракт домена.

    Нужен потому, что домен не должен импортировать прикладной слой знаний,
    а каталог при этом остаётся простой dataclass без наследования.
    """

    __slots__ = ("_source",)

    def __init__(self, source: catalog.Article) -> None:
        self._source = source

    @property
    def intent(self) -> Intent:
        return self._source.intent

    @property
    def title(self) -> str:
        return self._source.title

    @property
    def body(self) -> str:
        return self._source.body

    @property
    def keywords(self) -> tuple[str, ...]:
        return self._source.keywords


class StaticKnowledgeBase(KnowledgeBase):
    """База знаний, собранная из статического каталога.

    Поиск — по ключевым словам, без внешних зависимостей и без сети.
    Это делает ответы мгновенными и бесплатными; LLM подключается поверх
    только для формулировок там, где статьи не хватает.
    """

    def __init__(self, articles: tuple[catalog.Article, ...] = catalog.ARTICLES) -> None:
        self._articles: dict[Intent, list[_ArticleAdapter]] = {}
        for article in articles:
            self._articles.setdefault(article.intent, []).append(_ArticleAdapter(article))
        self._phrase_index, self._prefix_index = self._build_index()

    # -- KnowledgeBase --------------------------------------------------------
    def find(self, intent: Intent) -> list[KnowledgeArticle]:
        return list(self._articles.get(intent, ()))

    def render(self, intent: Intent) -> str:
        """Склеить тексты всех статей по намерению ("" — если статей нет)."""
        bodies = [a.body for a in self._articles.get(intent, ())]
        return "\n\n".join(bodies)

    def context_for_llm(self, intent: Intent) -> str:
        """Компактная выжимка по теме — именно она уходит в промпт модели.

        Жёстко ограничена по длине: в контекст LLM нельзя тащить
        справочник целиком, иначе вырастут стоимость и задержка.
        """
        text = self.render(intent)
        limit = 1200
        if len(text) <= limit:
            return text
        return text[:limit].rsplit("\n", 1)[0] + "\n[…]"

    # -- Дополнительный публичный API -----------------------------------------
    def match_intents(self, text: str) -> list[tuple[Intent, int]]:
        """Найти намерения, релевантные тексту, с весом совпадения.

        Учитывает русскую морфологию: ключевое слово «данные» находит
        «данными» и «данного» за счёт совпадения по началу слова.
        Минимальная длина приставки — 4 символа, чтобы не поймать
        случайные слова.

        Вес — число сработавших ключевых слов. Список отсортирован по
        убыванию веса, поэтому :mod:`chatbot.services.intent_resolver`
        может взять верхний элемент.
        """
        haystack = normalize(text)
        if not haystack:
            return []

        scores: dict[Intent, float] = {}

        # 1) Фразы: многословные ключи и точные совпадения.
        for phrase, intents in self._phrase_index.items():
            if " " in phrase and phrase in haystack:
                _add_score(scores, intents, 1.0)

        # 2) Слова: совпадение по началу слова.
        for word in _tokenize(haystack):
            for candidates in (
                self._prefix_index.get(word[:_MIN_PREFIX]),
                self._phrase_index.get(word),
            ):
                if candidates:
                    _add_score(scores, candidates, 0.8)

        return sorted(
            ((intent, round(score, 2)) for intent, score in scores.items()),
            key=lambda pair: pair[1],
            reverse=True,
        )

    def has_article(self, intent: Intent) -> bool:
        """Есть ли в базе хоть одна статья по намерению."""
        return bool(self._articles.get(intent))

    # -- Внутреннее -----------------------------------------------------------
    @staticmethod
    def _build_index() -> tuple[dict[str, list[Intent]], dict[str, list[Intent]]]:
        """Построить индексы: «фраза → намерения» и «начало слова → намерения»."""
        phrases: dict[str, list[Intent]] = {}
        prefixes: dict[str, list[Intent]] = {}
        for article in catalog.ARTICLES:
            for keyword in article.keywords:
                key = normalize(keyword)
                if not key:
                    continue
                _push(phrases, key, article.intent)
                if " " not in key:
                    _push(prefixes, key[:_MIN_PREFIX], article.intent)
        return phrases, prefixes


#: Минимальная длина совпадения по началу слова.
_MIN_PREFIX = 4
#: ``\w`` в Python 3 совместим с Unicode — это важно для кириллицы.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _push(index: dict[str, list[Intent]], key: str, intent: Intent) -> None:
    """Добавить намерение в индекс, не дублируя его."""
    bucket = index.setdefault(key, [])
    if intent not in bucket:
        bucket.append(intent)


def _add_score(scores: dict[Intent, float], intents: list[Intent], weight: float) -> None:
    """Прибавить вес каждому из намерений."""
    for intent in intents:
        scores[intent] = scores.get(intent, 0.0) + weight


def _tokenize(text: str) -> list[str]:
    """Разбить нормализованный текст на слова."""
    return _WORD_RE.findall(text)
