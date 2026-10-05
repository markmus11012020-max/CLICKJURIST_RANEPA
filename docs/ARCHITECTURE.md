# ClickJurist Production — Архитектура

## Структура проекта

```
backend/
  main.py               сборка приложения: middleware, роутеры, статика (~180 строк)
  startup_checks.py     production-валидация конфигурации до старта
  config.py             настройки (pydantic-settings)
  security.py           хеширование сессий, извлечение IP, ключи квоты
  db.py                 слой доступа к данным (сессии, квоты, платежи, журнал)
  jwt_auth.py           подписанные cookie сессии
  api/                  HTTP-слой, разбит по доменам
    deps.py             платёжный барьер (session_gate)
    router_query.py     консультация и фоновые задачи
    router_docs.py      чек-листы, документы, PDF
    router_payment.py   Robokassa
    router_auth.py      сессия / logout
    router_meta.py      тарифы, здоровье, статистика, SPA
  services/             бизнес-логика
    prompts/            тексты промптов, разбиты по доменам
    ...
chatbot/                опциональный модуль чат-бота (см. chatbot/README.md)
  domain/               сущности и интерфейсы, без внешних зависимостей
  knowledge/            база знаний (работает без LLM)
  llm/                  шлюз к LLM и потоковая генерация
  services/             оркестратор, сценарии, санитайзер
  api/                  маршруты чат-бота
  static/               виджет на ES-модулях, без сборщика
frontend/
  index.html
  script.js             тонкая точка входа
  js/
    core/               dom, net, markdown, state
    ui/                 сессия, дисклеймер, paywall
    features/           streaming, query, checklist, document, pdf, package
```

Слои зависят только внутрь: `domain → services → api`. Обратных импортов нет.
Модуль `chatbot/` подключается через `register_chatbot(app)` в `try/except` —
если он падает, сайт продолжает работать без чат-бота.

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
- `network_quota(network_hash, free_requests_used, updated_at)` — счётчик по IP;
- `payments(inv_id, session_hash, service, amount, status, created_at, paid_at)`;
- `request_log(session_hash, service, status_code, is_free, provider, latency_ms, created_at)`.

Никаких ФИО, адресов, телефонов, текстов запросов и ответов.

## Бесплатный лимит: два независимых ключа

`session_gate` (`backend/api/deps.py`) — единственное место, где решается,
пройдёт ли запрос. Он проверяет квоту по **двум** независимым ключам:

| Ключ | Хеш | Смысл |
|---|---|---|
| Сессионный | `SHA-256(IP + fingerprint + SALT)` | конкретный браузер |
| Сетевой | `SHA-256("ip-level" + IP + SALT)` | адрес сети |

Зачем второй уровень: `fingerprint` приходит из клиентского заголовка
`X-Client-Fingerprint`, то есть полностью контролируется посетителем. Один
браузерный ключ обходится сменой значения заголовка или очисткой
localStorage. Сетевой ключ от этого не зависит, поэтому бесплатный лимит
нельзя обойти бесконечно — он ограничен `FREE_TIER_IP_REQUESTS`.

Значение сетевого лимита намеренно **больше** браузерного: за одним IP
живут целые офисы, мобильные операторы и домашние сети за NAT, и лимит
«1 запрос на IP» заблокировал бы их всех сразу. Стартовая проверка
предупредит, если `FREE_TIER_IP_REQUESTS < FREE_TIER_REQUESTS`.

Порядок проверок в `session_gate`:

1. `DEV_BYPASS_PAYWALL` — обход всего (только явное включение);
2. `has_paid_access` — оплата не зависит ни от одного ключа;
3. квота по сессионному ключу, затем по сетевому;
4. иначе — 402 с инвойсом Robokassa.

Сетевой счётчик списывается в момент выдачи доступа, браузерный — в
эндпоинте (`store.consume_free_request`).

## Безопасность (152-ФЗ)

1. **Идентификация сессии** = `SHA-256(IP + fingerprint + SESSION_HASH_SALT)`.
2. **Квота дублируется** сетевым ключом = `SHA-256("ip-level|" + IP + SALT)`,
   чтобы клиентский отпечаток нельзя было использовать для обхода лимита.
3. **В логах** — только `anonymized_session_id` = UUIDv5 от хеша.
4. **Маскировка ПДн** — двойной контур: LLM + regex-страховка.
5. **KMS** — production-секреты расшифровываются из Yandex KMS при старте.
6. **Zero-Storage Logging** — структурированный JSON в Yandex Cloud Logging.

## Failover-оркестратор

`PRIMARY_PROVIDER=aitunnel` ⇒ резерв `yandex`;
`PRIMARY_PROVIDER=yandex`   ⇒ резерв `aitunnel`.

Любая сетевая аномалия или HTTP 5xx ⇒ `LLMError` ⇒ автоматический переход на резерв.
