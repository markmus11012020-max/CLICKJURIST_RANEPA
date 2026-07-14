"""Логика вызовов LLM через AITUNNEL.

Используется ОФИЦИАЛЬНЫЙ OpenAI-совместимый эндпоинт (см. https://docs.aitunnel.ru/):
  POST https://api.aitunnel.ru/v1/chat/completions
  Authorization: Bearer <ROUTER_API_KEY>
  Content-Type: application/json
  Body: {
    "model": "<model_id>",                       # например "deepseek-v4-flash" или "minimax-m3"
    "messages": [{"role":"system|user|assistant", "content":"..."}],
    "temperature": 0.2,                          # по ТЗ для LLM-1
    "max_tokens": 8000                           # разумный потолок (по факту 50000 приводит к таймауту)
  }
  Ответ: response_json["choices"][0]["message"]["content"]
  Также доступен блок response_json["usage"] = {prompt_tokens, completion_tokens, total_tokens}

ВАЖНО про max_tokens:
  В ТЗ указан лимит 50000, но модель minimax-m3 при таком max_tokens тратит >120 c на ответ
  (использует много reasoning_tokens). Эмпирически 8000 токенов ≈ 30 000 символов текста —
  этого достаточно для полноценной юридической консультации. В .env это можно переопределить
  переменной LLM2_MAX_TOKENS. Таймаут 600 c, чтобы пережить «медленные» reasoning-модели.

ЛОГИРОВАНИЕ:
  Каждый вызов _post_chat пишет строку в usage.jsonl (см. services.usage).
  Через параметр `endpoint` помечаем, из какого API пришёл запрос (generate / checklist /
  document / pdf). Это позволяет потом считать расходы по эндпоинтам.
"""
import time
import requests

from clerk_craft.backend.config import settings
from clerk_craft.backend.services.usage import log_usage

API_URL = "https://api.aitunnel.ru/v1/chat/completions"
TIMEOUT_S = 600  # сек; модель может долго «рассуждать»
MAX_TOKENS_LLM_1 = 4000
MAX_TOKENS_LLM_2 = 6000  # default, переопределяется через .env LLM2_MAX_TOKENS
# Сколько раз повторять запрос, если провайдер вернул content: null
# (наблюдается на AITUNNEL при перегрузке reasoning-модели)
RETRY_ON_NULL = 2
RETRY_SLEEP_S = 5

# Маркер «ошибочной строки» от _post_chat, чтобы отличать контент от ошибки
_ERROR_PREFIX = "Ошибка"


# =====================================================================================
# Промпты
# =====================================================================================
PROMPT_LLM_1 = """Роль: Ты — квалифицированный юрист, действующий в соответствии с законодательством Российской Федерации.
Задача: Проведи правовой анализ ситуации и дай консультацию, опираясь на следующие обязательные нормы и правила:
1. Законодательная база: Действующее законодательство РФ (Конституция РФ, Гражданский, Уголовный, Трудовой или иной профильный кодекс, применимый к ситуации).
2. Актуальность: Использовать только действующие редакции нормативно-правовых актов, с учетом последних изменений и постановлений Пленума Верховного Суда РФ.
3. Профессиональная этика: Соблюдать принципы конфиденциальности (защита персональных данных согласно Федеральному закону о персональных данных) и объективности (как требует Кодекс профессиональной этики адвоката).
4. Судебная практика: Опираться на сложившуюся правоприменительную практику и прецеденты, отраженные в системе КонсультантПлюс или аналогичных правовых базах.

Контекст ситуации клиента: [Сюда подставляется описание проблемы, вопрос или обстоятельства дела]
Формат ответа: Четкая юридическая квалификация, правовые риски, возможные варианты решения проблемы со ссылками на статьи законов и пошаговым алгоритмом действий.

ЖЁСТКИЕ ПРАВИЛА:
— Не придумывай номера дел, конкретные суммы компенсаций или точные размеры госпошлин. Размер госпошлины может меняться — если он нужен в ответе, пиши в скобках «размер актуальной пошлины можно уточнить в момент подачи исковых требований».
— Не выдумывай статьи и их номера. Если не уверен в номере — пиши «указанная норма» или «статья о защите прав потребителей» без номера.
— Не снижай качество ответа из-за того, что вопрос задан простым языком — отвечай на высоком профессиональном уровне, но точно по сути.
— Общий объём ответа — не более 150 слов. Пиши максимально емко, без «воды» и пустых фраз-паразитов.
— Соблюдай принцип АНОНИМНОСТИ: никаких реальных или вымышленных ФИО, адресов и названий компаний.

ГЛАВНЫЕ ПРИНЦИПЫ:
— Говоришь по-русски, кириллицей. Никакой транслитерации.
— Не повторяешь вопрос пользователя — сразу даёшь ответ. Без лишних вежливых вступлений и дисклеймеров.
— Сложные юридические термины объясняешь в скобках простыми словами.
— Если в вопросе есть пробелы (не указаны даты, суммы, стороны) — не выдумывай их, а задай 1–2 уточняющих вопроса в самом конце."""


PROMPT_LLM_2 = """Ты — опытный юрист-консультант и Senior-асессор проекта по оценке качества ИИ.

ВХОД: вопрос пользователя и черновик ответа от LLM-1.

ТВОЯ ЗАДАЧА: выполнить внутреннюю проверку черновика по 5 пунктам:
  1) Проверка нормативной базы (статьи, галлюцинации, толкование).
  2) Проверка судебной практики (дела/постановления ВС РФ, ВАС РФ, КС РФ).
  3) Проверка расчётов (сроки, пошлины, неустойки, проценты по ст. 395 ГК РФ).
  4) Удаление воды, дисклеймеров, фраз-паразитов.
  5) Сборка финального эталона.

❗️ОЧЕНЬ ВАЖНО — ФОРМАТ ОТВЕТА:
- НЕ выводи в ответ шаги 1–4. Они нужны ТОЛЬКО для твоей внутренней проверки.
- В ответе пользователю — ТОЛЬКО финальный результат шага 5, БЕЗ упоминания LLM-1, БЕЗ служебных заголовков «Проверка нормативной базы» и т. п.
- ВСЕГО ОДИН ЗАГОЛОВОК В ОТВЕТЕ — строго: ## Итоговый ответ
- Сразу под этим заголовком — сам ответ: абзацы, при необходимости пояснительные подзаголовки (### ), пояснительные списки.
- НЕ включай чек-лист и нумерованный список «пошаговых действий» в виде чек-боксов — это отдельный инструмент, его заказывают отдельной кнопкой.
- Объём — 400–700 слов. Стиль — юридически точный, но понятный человеку без юр. образования. Никакой транслитерации. Только русский, кириллица.

ПИШИ ТОЛЬКО:
## Итоговый ответ
<далее сам текст консультации>"""


PROMPT_CHECKLIST = """Ты — юрист-консультант. На основе вопроса пользователя и финального ответа LLM-2 составь краткий чек-лист практических действий для человека, которому нужна юридическая помощь.

ПРАВИЛА:
— Только конкретные, проверяемые шаги (что взять, куда подать, кого вызвать, в какой срок).
— 5–10 пунктов, не больше.
— Каждый пункт начинается с глагола в инфинитиве («Подготовить…», «Собрать…», «Подать…», «Зафиксировать…»).
— Указывай реальные сроки (дни/месяцы) и места (орган/инстанция), если они вытекают из закона; не выдумывай конкретные номера дел и точные суммы пошлин.
— Если в вопросе не хватает данных — перечисли, какие документы/сведения нужно собрать пользователю, чтобы двигаться дальше.
— НЕ пиши дисклеймеров и «воды». НЕ повторяй юридическое обоснование из основного ответа — это операционный план, а не теория.
— Язык: русский, кириллица. Без транслитерации.

ФОРМАТ ОТВЕТА — строго Markdown-список c чекбоксами:
- [ ] Шаг 1 …
- [ ] Шаг 2 …
- [ ] Шаг 3 …
…"""


PROMPT_DOCUMENT = """Ты — практикующий юрист, который составляет процессуальные документы по законодательству РФ.
Пользователь прислал: (1) описание своей ситуации и (2) тип документа, который нужно составить: {doc_type_ru}.

ЗАДАЧА: Подготовь полноценный, грамотно оформленный {doc_type_ru} со всеми обязательными реквизитами.

ОБЯЗАТЕЛЬНАЯ СТРУКТУРА {doc_type_ru}:

1. Шапка (кому/от кого). Используй плейсхолдеры в квадратных скобках:
   - Для искового заявления: [Наименование суда], [ФИО истца / адрес / телефон], [ФИО или наименование ответчика / адрес], [цена иска (если применимо)], [госпошлина (размер актуальной пошлины уточняется при подаче)].
   - Для претензии: [ФИО / наименование адресата — руководитель организации], [адрес организации], [от кого: ФИО / адрес / телефон], [номер договора / документа, если есть].
   - Для жалобы: [наименование органа/должностного лица, в адрес которого подаётся жалоба], [от кого: ФИО / адрес / телефон], [на кого/на что: ФИО должностного лица или наименование органа, чьи действия обжалуются].

2. Заголовок документа: «ИСКОВОЕ ЗАЯВЛЕНИЕ» / «ПРЕТЕНЗИЯ» / «ЖАЛОБА» — по центру.

3. Описательная часть: кратко, но содержательно изложи обстоятельства дела — что произошло, какие права нарушены, какие доказательства имеются. Только факты, без эмоций.

4. Правовое обоснование: укажи конкретные нормы законодательства РФ, на которых основывается требование. Не выдумывай номера статей — если не уверен, пиши «[соответствующая норма …]».

5. Просительная часть: чётко сформулируй требования (что именно просите: взыскать сумму, обязать совершить действие, признать право, отменить решение и т. п.).

6. Приложения: перечисли документы, которые прикладываются (копия договора, квитанции, переписка, фото, акт и т. п.). Используй плейсхолдеры [перечень документов].

7. Дата и подпись: [«__» ________ 20__ г.] и [____________________ / ФИО].

ЖЁСТКИЕ ПРАВИЛА:
— Пиши только на русском, кириллицей. Никакой транслитерации.
— Соблюдай принцип АНОНИМНОСТИ: никаких реальных или вымышленных ФИО, адресов, названий компаний — только плейсхолдеры в квадратных скобках.
— Не указывай конкретные номера судебных дел и точные суммы компенсаций/госпошлин.
— Стиль — официально-деловой, без эмоций, без «воды».
— Объём — 400–700 слов, достаточно для рабочего шаблона, который пользователь сможет заполнить своими данными.
— Не добавляй раздел «Советы / Чек-лист» — это отдельный инструмент.
— Формат ответа — чистый Markdown (заголовки, списки), без HTML-тегов."""


# =====================================================================================
# Низкоуровневый POST в AITUNNEL
# =====================================================================================
def _post_chat(
    messages: list,
    model: str,
    temperature: float,
    max_tokens: int,
    endpoint: str = "unknown",
) -> tuple[str, dict]:
    """POST в AITUNNEL chat/completions. Возвращает (content, usage_dict).

    content: текст ответа ИЛИ строка-ошибка (начинается с «Ошибка: …»).
    usage_dict: {"prompt_tokens", "completion_tokens", "total_tokens"} (нули при ошибке).

    При получении «пустой content (None)» — делает до RETRY_ON_NULL повторных попыток.
    Каждый фактический вызов пишется в usage.jsonl (включая ошибки — с ok=False).
    """
    headers = {
        "Authorization": f"Bearer {settings.ROUTER_API_KEY.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    for attempt in range(1 + RETRY_ON_NULL):
        t_start = time.monotonic()
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT_S)
        except requests.exceptions.Timeout:
            latency = time.monotonic() - t_start
            log_usage(endpoint=endpoint, model=model,
                      prompt_tokens=0, completion_tokens=0,
                      latency_s=latency, ok=False, error=f"timeout {TIMEOUT_S}s")
            return (
                f"{_ERROR_PREFIX}: таймаут {TIMEOUT_S} c при обращении к AITUNNEL",
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
        except requests.exceptions.ConnectionError as e:
            latency = time.monotonic() - t_start
            log_usage(endpoint=endpoint, model=model,
                      prompt_tokens=0, completion_tokens=0,
                      latency_s=latency, ok=False, error=f"connection: {e}")
            return (
                f"{_ERROR_PREFIX}: сеть/API недоступно ({e})",
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
        except requests.exceptions.RequestException as e:
            latency = time.monotonic() - t_start
            log_usage(endpoint=endpoint, model=model,
                      prompt_tokens=0, completion_tokens=0,
                      latency_s=latency, ok=False, error=str(e))
            return (
                f"{_ERROR_PREFIX}: {e}",
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

        latency = time.monotonic() - t_start

        if r.status_code >= 400:
            log_usage(endpoint=endpoint, model=model,
                      prompt_tokens=0, completion_tokens=0,
                      latency_s=latency, ok=False, error=f"http {r.status_code}: {r.text[:200]}")
            return (
                f"{_ERROR_PREFIX} API ({r.status_code}): {r.text[:500]}",
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log_usage(endpoint=endpoint, model=model,
                      prompt_tokens=0, completion_tokens=0,
                      latency_s=latency, ok=False, error=f"json: {e}")
            return (
                f"{_ERROR_PREFIX}: невалидный JSON ({e}). Тело: {r.text[:500]}",
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

        # Извлекаем usage (если провайдер отдал)
        u = data.get("usage") or {}
        prompt_tokens = int(u.get("prompt_tokens") or 0)
        completion_tokens = int(u.get("completion_tokens") or 0)
        total_tokens = int(u.get("total_tokens") or (prompt_tokens + completion_tokens))
        usage_dict = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log_usage(endpoint=endpoint, model=model,
                      prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                      latency_s=latency, ok=False, error="no choices[0].message.content")
            return (
                f"{_ERROR_PREFIX}: в ответе нет choices[0].message.content. Тело: {r.text[:500]}",
                usage_dict,
            )

        if content is None or not str(content).strip():
            err = f"попытка {attempt + 1}/{1 + RETRY_ON_NULL}: провайдер вернул пустой content (None)"
            print(f"[llm_chain] {model}: {err}, повтор через {RETRY_SLEEP_S} c", flush=True)
            # Пишем usage для неудачной попытки (без content), но НЕ считаем её финальной
            log_usage(endpoint=endpoint, model=model,
                      prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                      latency_s=latency, ok=False, error="empty content (will retry)")
            time.sleep(RETRY_SLEEP_S)
            continue

        # Успешный ответ — пишем usage и возвращаем
        log_usage(endpoint=endpoint, model=model,
                  prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                  latency_s=latency, ok=True)
        return content, usage_dict

    return (
        f"{_ERROR_PREFIX}: провайдер упорно возвращает пустой content после {1 + RETRY_ON_NULL} попыток",
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    )


# =====================================================================================
# Высокоуровневые обёртки
# =====================================================================================
def call_llm1(user_query: str, endpoint: str = "generate") -> str:
    """Шаг 1: Юрист-консультант (черновик). temperature=0.2, max_tokens=4000."""
    content, _usage = _post_chat(
        messages=[
            {"role": "system", "content": PROMPT_LLM_1},
            {"role": "user", "content": f"Контекст ситуации клиента: {user_query}"},
        ],
        model=settings.MODEL_LLM_1,
        temperature=0.2,
        max_tokens=MAX_TOKENS_LLM_1,
        endpoint=endpoint,
    )
    return content


def _strip_to_final_answer(text: str) -> str:
    """Страховка: если LLM-2 всё-таки прислала служебные шаги 1–4,
    оставляем только блок начиная с «## Итоговый ответ» (или «## 5. Итоговый эталонный ответ»).
    Если маркера нет — возвращаем текст как есть.
    """
    if not text:
        return text
    markers = [
        "## Итоговый ответ",
        "## Итоговый эталонный ответ",
        "## 5. Итоговый эталонный ответ",
        "## 5. Итоговый ответ",
        "**Итоговый ответ**",
        "**Итоговый эталонный ответ**",
    ]
    for m in markers:
        idx = text.find(m)
        if idx != -1:
            return text[idx:].strip()
    return text.strip()


def call_llm2(user_query: str, draft_text: str, endpoint: str = "generate") -> str:
    """Шаг 2: Senior-асессор. max_tokens=8000, температура 0.2."""
    raw, _usage = _post_chat(
        messages=[
            {"role": "system", "content": PROMPT_LLM_2},
            {
                "role": "user",
                "content": (
                    f"Вопрос пользователя: {user_query}\n\n"
                    f"Ответ LLM-1 (черновик):\n{draft_text}"
                ),
            },
        ],
        model=settings.MODEL_LLM_2,
        temperature=0.2,
        max_tokens=MAX_TOKENS_LLM_2,
        endpoint=endpoint,
    )
    return _strip_to_final_answer(raw)


def run_llm_pipeline(user_query: str, endpoint: str = "generate") -> dict:
    """Двухэтапный пайплайн: LLM-1 → LLM-2. Возвращает raw_draft и final_verified.
    Параметр endpoint прокидывается в usage.jsonl (для /api/generate и /api/generate-pdf —
    разные теги)."""
    try:
        draft = call_llm1(user_query, endpoint=endpoint)
        if not draft or not isinstance(draft, str):
            return {"error": f"Ошибка LLM-1: пустой или невалидный ответ ({type(draft).__name__})"}
        if draft.startswith("Ошибка"):
            return {"error": f"Ошибка LLM-1: {draft}"}

        final = call_llm2(user_query, draft, endpoint=endpoint)
        if not final or not isinstance(final, str):
            return {
                "raw_draft": draft,
                "final_verified": draft,
                "degraded": True,
                "warning": f"LLM-2 (асессор) не ответила: пустой или невалидный результат ({type(final).__name__}). Показан черновик LLM-1 без второй проверки.",
            }
        if final.startswith("Ошибка"):
            return {
                "raw_draft": draft,
                "final_verified": draft,
                "degraded": True,
                "warning": f"LLM-2 (асессор) не ответила: {final}. Показан черновик LLM-1 без второй проверки.",
            }

        return {"raw_draft": draft, "final_verified": final}
    except Exception as e:
        return {"error": f"Сбой сети или сервера: {e}"}


def generate_checklist(user_query: str, final_answer: str) -> str:
    """Генерация чек-листа по финальному ответу LLM-2. Использует LLM-1 — она быстрая и дешёвая."""
    prompt_user = (
        f"Вопрос пользователя:\n{user_query}\n\n"
        f"Финальный ответ юриста (LLM-2):\n{final_answer}\n\n"
        "Составь пошаговый чек-лист практических действий для пользователя строго по формату из системного промпта."
    )
    content, _usage = _post_chat(
        messages=[
            {"role": "system", "content": PROMPT_CHECKLIST},
            {"role": "user", "content": prompt_user},
        ],
        model=settings.MODEL_LLM_1,
        temperature=0.1,
        max_tokens=2000,
        endpoint="checklist",
    )
    return content


def generate_document(user_query: str, doc_type: str) -> str:
    """Генерация шаблона документа (иск / претензия / жалоба) по запросу клиента.
    doc_type: 'isk' | 'pretension' | 'zhaloba'."""
    allowed = {"isk": "исковое заявление", "pretension": "претензия", "zhaloba": "жалоба"}
    if doc_type not in allowed:
        return f"Ошибка: неизвестный тип документа '{doc_type}'. Допустимо: {list(allowed)}."
    doc_type_ru = allowed[doc_type]

    system = PROMPT_DOCUMENT.format(doc_type_ru=doc_type_ru)
    prompt_user = f"Ситуация клиента: {user_query}\n\nСоставь {doc_type_ru} строго по структуре из системного промпта."

    content, _usage = _post_chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt_user},
        ],
        model=settings.MODEL_LLM_2,
        temperature=0.15,
        max_tokens=4000,
        endpoint=f"document_{doc_type}",
    )
    return content
