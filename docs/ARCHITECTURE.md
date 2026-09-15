# ClickJurist Production — Архитектура

## Поток обработки запроса

```
Клиент (SPA)
    │ POST /api/query { query }
    ▼
Платёжный барьер: 1 бесплатно, далее 402 + Robokassa
    ▼
STAGE 1 (контур РФ)
   YandexGPT | Ollama | regex → маскировка ПДн + анонимное резюме
    │ передаётся [NAME_1], [ADDRESS_1] — НЕ оригинальные данные
    ▼
Web-фактчекинг (от 2 до 3 независимых источников)
    Yandex Search | Serper | Tavily → блок SOURCES
    ▼
STAGE 2 (внешний контур)
   AITunnel → Gemini 2.5 Flash (LLM-1: черновик ≤150 слов)
    │
    ▼
   AITunnel → Senior-асессор (LLM-2: эталон 400–700 слов)
    │
    ▼
Дисклеймер + X-Session-Id → клиенту
```

## Хранилище

* development → SQLite (файл `data/clickjurist.db`);
* production → Yandex Managed PostgreSQL.

В БД хранятся **только**:
- `sessions(session_hash, is_free, free_requests_used, requests_total, paid_until)`;
- `payments(inv_id, session_hash, service, amount, status, created_at, paid_at)`;
- `request_log(session_hash, service, status_code, is_free, provider, latency_ms, created_at)`.

Никаких ФИО, адресов, телефонов, текстов запросов и ответов.

## Безопасность (152-ФЗ)

1. **Идентификация сессии** = `SHA-256(IP + fingerprint + SESSION_HASH_SALT)`.
2. **В логах** — только `anonymized_session_id` = UUIDv5 от хеша.
3. **Маскировка ПДн** — двойной контур: LLM + regex-страховка.
4. **KMS** — production-секреты расшифровываются из Yandex KMS при старте.
5. **Zero-Storage Logging** — структурированный JSON в Yandex Cloud Logging.

## Failover-оркестратор

`PRIMARY_PROVIDER=aitunnel` ⇒ резерв `yandex`;
`PRIMARY_PROVIDER=yandex`   ⇒ резерв `aitunnel`.

Любая сетевая аномалия или HTTP 5xx ⇒ `LLMError` ⇒ автоматический переход на резерв.
