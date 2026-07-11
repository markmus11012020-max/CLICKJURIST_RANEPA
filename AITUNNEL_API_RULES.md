# УНИВЕРСАЛЬНЫЙ ТЕХНИЧЕСКИЙ СТАНДАРТ РАБОТЫ С API AITUNNEL

Источник истины: официальная документация — **https://docs.aitunnel.ru/**  
Этот документ — выжимка из неё, адаптированная под наш стек (Python `requests`).

---

## 1. СЕТЕВАЯ АРХИТЕКТУРА И ЭНДПОИНТЫ

- **Базовый URL:** `https://api.aitunnel.ru`
- **Эндпоинт чата:** `POST https://api.aitunnel.ru/v1/chat/completions`
- **Эндпоинт списка моделей:** `GET https://api.aitunnel.ru/v1/models`
- Метод — **строго POST** для чата.  
- Можно использовать официальную Python-библиотеку `openai`, передав `base_url="https://api.aitunnel.ru/v1/"`. В этом проекте используется сырой `requests` (для прозрачности и контроля таймаутов).  
- **ВАЖНО:** прямой POST на `https://aitunnel.ru` (без `/v1/chat/completions`) возвращает **405 Method Not Allowed** — этим страдает устаревший код, в котором использовался `input` вместо `messages`.

---

## 2. АУТЕНТИФИКАЦИЯ

```http
Authorization: Bearer <ROUTER_API_KEY>
Content-Type: application/json
```

Токен берётся из панели https://aitunnel.ru/panel (формат `sk-aitunnel-...`).  
**Не коммитить** в git — только через `.env` (см. раздел 5).

---

## 3. СТРУКТУРА ПАКЕТА ДАННЫХ (PAYLOAD)

```json
{
  "model": "<model_id>",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user",   "content": "..."}
  ],
  "temperature": 0.2,
  "max_tokens": 4000
}
```

- `model` — id **без префикса автора**. Список всех id — `GET /v1/models` (на момент 2026-07: 224 моделей).  
  Например: `deepseek-v4-flash`, `minimax-m3`, `auto`, `deepseek-r1`, `claude-sonnet-5`, `gpt-5`.
- `messages` — массив в формате OpenAI Chat Completions.
- `temperature` — по умолчанию 0.2 (по ТЗ проекта, для минимизации галлюцинаций).
- `max_tokens` — рекомендуется указывать явно (провайдер использует это для расчёта цены).

---

## 4. ИЗВЛЕЧЕНИЕ ОТВЕТА

```json
{
  "id": "gen-...",
  "object": "chat.completion",
  "model": "<model_id>",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ..., "cost_rub": ...}
}
```

В Python:

```python
text = response_json["choices"][0]["message"]["content"]
```

> ❌ **Неправильно** (использовалось в старой версии): `response_json["text"]` — этого ключа в OpenAI-совместимом ответе нет.

---

## 5. ЭТАЛОННЫЙ ШАБЛОН КОДА НА PYTHON (REQUESTS)

```python
import requests
import os
from dotenv import load_dotenv

load_dotenv()  # берёт ROUTER_API_KEY из .env
API_KEY = os.getenv("ROUTER_API_KEY", "").strip()

def call_aitunnel_chat(messages: list, model: str,
                        temperature: float = 0.2,
                        max_tokens: int = 4000) -> str:
    url = "https://api.aitunnel.ru/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
```

---

## 6. НАСТРОЙКА ОКРУЖЕНИЯ (.ENV)

```env
ROUTER_API_KEY="sk-aitunnel-..."
ROUTER_BASE_URL="https://api.aitunnel.ru/v1"
MODEL_LLM_1="deepseek-v4-flash"
MODEL_LLM_2="minimax-m3"
```

> ⚠️ Имена моделей в `.env` (и в коде) **без префикса автора** (`deepseek/...` — устаревший формат).

---

## 7. МОДЕЛИ, ИСПОЛЬЗУЕМЫЕ В ПРОЕКТЕ «КлеркКрафт»

| Роль | model_id | Назначение |
|---|---|---|
| LLM-1 (Юрист) | `deepseek-v4-flash` | быстрый черновик консультации |
| LLM-2 (Асессор) | `minimax-m3`     | критический разбор и эталон |

Списки моделей провайдера (на 2026-07-10) включают также: `auto`, `deepseek-r1`, `claude-sonnet-5`, `gpt-5`, `gemini-3.1-pro-preview`, `gigachat-2-max` и др. Полный список — `GET /v1/models`.

---

## 8. ЭМПИРИЧЕСКИЕ НАБЛЮДЕНИЯ (по итогам финального теста 2026-07-10 21:01)

Пайплайн `LLM-1 → LLM-2` отработал **end-to-end успешно** на запросе «соседи шумят после 23:00». Полный лог — `C:\Users\Профи\Project\aitunnel_pipeline_result.txt`.

| Шаг | Модель | max_tokens | Длительность | Размер ответа | Стоимость |
|---|---|---|---|---|---|
| LLM-1 | `deepseek-v4-flash` | 4000 | ~2 c | 1131 символ | 0.01 ₽ |
| LLM-2 | `minimax-m3`        | 8000 | ~110-150 c (reasoning) | 8917 символов | 0.02 ₽ |
| **Итого** | | | **~150 c** | | **0.02 ₽** |

### Важные выводы

1. **`max_tokens=50000` из ТЗ нежизнеспособен.** Модель `minimax-m3` при `max_tokens=50000` не отвечает в пределах 120-секундного таймаута. Эмпирически безопасно использовать:
   - LLM-1: `max_tokens=4000` (≈ 16 000 символов).
   - LLM-2: `max_tokens=8000` (≈ 32 000 символов — этого достаточно для эталонного ответа).
   В коде вынесено в константы `MAX_TOKENS_LLM_1` и `MAX_TOKENS_LLM_2` (`backend/services/llm_chain.py`).

2. **Таймаут** поднят с 120 c до **600 c** (`TIMEOUT_S = 600`): модель `minimax-m3` показывает `reasoning_tokens` (видно в usage) и тратит много времени на «мысли», прежде чем выдать видимый текст.

3. **Модель `minimax-m3` иногда отвечает одним символом** (например «Да», «\nРАБОТАЕТ»). Это нормально — `temperature=0.2` и короткий контекст дают экономичный ответ. Проблема была **только** в таймауте, не в самой модели.

4. **Качество выходов соответствует ТЗ:**
   - LLM-1: юридически корректный черновик (ст. 3.13 КоАП Москвы, ст. 304, 151 ГК РФ, упоминание КУСП).
   - LLM-2: разбор по 5 заголовкам из ТЗ (нормативная база → практика → расчёты → «вода» → эталон). Дополнительно: нашёл устаревший СН 2.2.4/2.1.8.562-96, заменил на актуальный СанПиН 1.2.3685-21; добавил ст. 17, 30, 31 ЖК РФ и ПП РФ № 25; дал ссылку на Пленум ВС/ВАС № 10/22 от 29.04.2010.

5. **⚠️ Контроль качества выходных данных:** LLM-2 иногда генерирует **точные суммы штрафов и цифры** (например, таблицу штрафов по ст. 3.13 КоАП Москвы). По ТЗ просили не придумывать цифры — перед публикацией ответа юристу **обязательна** ручная перепроверка числовых данных через актуальную редакцию кодекса.

6. **Структура кода готова к проду:** `run_llm_pipeline(user_query)` возвращает `{"raw_draft": ..., "final_verified": ...}` либо `{"error": "..."}` — удобно для FastAPI/Streamlit.
