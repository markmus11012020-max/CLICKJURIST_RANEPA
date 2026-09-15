# ClickJurist Production

Юридический ИИ-сервис: двухэтапный мегапайплайн (LLM-1 → LLM-2) с обезличиванием
персональных данных в контуре РФ, веб-фактчекингом и оплатой через Робокассу.

Полное ТЗ: [prompt.150926.md](prompt.150926.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Архитектура за 30 секунд

```
Клиент (SPA) ─▶ POST /api/query
                     │
       ┌─────────────┼──────────────────────────┐
       ▼             ▼                          ▼
 Платёжный     STAGE 1 (контур РФ)        Web-фактчекинг
   барьер      YandexGPT/Ollama/regex      Yandex/Serper/Tavily
       │             │                          │
       └─────────────┴────────────┬─────────────┘
                                  ▼
                       STAGE 2 (внешний)
                       AITunnel: LLM-1 → LLM-2
                                  │
                                  ▼
                       Дисклеймер → клиенту
```

## Структура репозитория

```
.
├── backend/                # FastAPI + бизнес-логика
│   ├── main.py             # HTTP-слой, эндпоинты, middleware
│   ├── config.py           # Настройки (env + KMS bootstrap)
│   ├── db.py               # SQLite/PostgreSQL (No-Data-Retention)
│   ├── models.py           # Pydantic-схемы запросов/ответов
│   ├── security.py         # 152-ФЗ: SHA-256 идентификация сессий
│   ├── logging_setup.py    # Zero-Storage Logging + PII-фильтр
│   ├── yandex_kms.py       # Шифрование секретов через Yandex KMS
│   └── services/
│       ├── pii_masker.py   # STAGE 1: маскировка ПДн
│       ├── web_factcheck.py# Web-поиск (2–3 источника)
│       ├── llm_chain.py    # Мегапайплайн + failover
│       ├── prompts.py      # Системные промпты (LLM-1/LLM-2/etc.)
│       ├── providers.py    # AITunnel / YandexGPT / Ollama
│       ├── robokassa.py    # Эквайринг (создание счёта, подписи)
│       └── pdf_generator.py# PDF с кириллицей (reportlab)
├── frontend/               # SPA (HTML/CSS/JS, без зависимостей)
├── tests/                  # pytest: безопасность, пайплайн, платежи
├── deploy/yandex-cloud/    # Dockerfile и инструкции для Cloud
├── docs/                   # Архитектура, эксплуатация, безопасность
├── data/                   # SQLite-БД (только development)
├── secrets/                # Шифротексты KMS (production)
├── .env.example            # Шаблон конфигурации (12 разделов)
└── prompt.150926.md        # Исходное ТЗ
```

## Быстрый старт (локально)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
cp .env.example .env
python -m backend.main
```

Сервис поднимется на http://localhost:8000. Документация API: `/api/docs`.

**Windows (автоматически):** достаточно запустить `start.bat` — скрипт
остановит старые процессы, очистит кэш, создаст/активирует venv,
установит зависимости и запустит сервер.

## Отказоустойчивость (failover)

Пайплайн построен на трёх провайдерах LLM и гарантирует ответ даже при
падении одного из внешних API:

| Этап         | Основной провайдер      | Резервный (failover)         |
|--------------|------------------------|------------------------------|
| Stage 1      | `PRIMARY_PROVIDER`       | второй провайдер             |
| Stage 2      | `yandex`               | `aitunnel` (Gemini, всегда) |
| Черновик/Эталон | `PRIMARY_PROVIDER`   | второй провайдер             |

- Переключение между `PRIMARY_PROVIDER=aitunnel` и `PRIMARY_PROVIDER=yandex`
  задаётся в `.env`.
- Stage 2 использует специальную цепочку `stage2_fallback_chain()`,
  которая гарантирует, что `aitunnel` (Gemini 2.5 Flash) всегда
  является финальным fallback — даже если Yandex упал (например,
  `UnicodeEncodeError` из-за кириллицы в заголовках).
- Защита HTTP-заголовков: все значения заголовков проходят через
  `_ascii_safe()` — кириллица и non-ASCII символы удаляются до
  отправки, предотвращая `UnicodeEncodeError` в библиотеке `requests`.

## Безопасность (152-ФЗ)

- Идентификация сессии: `SHA-256(IP + fingerprint + SESSION_HASH_SALT)`, соль в KMS.
- В логах — только анонимизированный UUID; сырые IP и текст запросов не сохраняются.
- Персональные данные маскируются российским LLM либо regex-страховкой **до** внешней модели.
- Секреты шифруются Yandex KMS (production) или хранятся в `.env` (development).

## Тарифы (по умолчанию)

| Услуга                          | Цена |
|---------------------------------|------|
| Юридическая консультация        | 99 ₽ |
| Чек-лист действий               | 100 ₽ |
| Шаблон документа / PDF-версия   | 300 ₽ |

Первый запрос — бесплатный.

## Тесты

```bash
pip install pytest
pytest -q
```

## Документация

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — поток данных и схема БД.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — запуск, переменные, мониторинг.
- [docs/SECURITY.md](docs/SECURITY.md) — соответствие 152-ФЗ.
- [docs/ROADMAP.md](docs/ROADMAP.md) — план развития.
- [deploy/yandex-cloud/README.md](deploy/yandex-cloud/README.md) — production-развёртывание.
- [docs/AITUNNEL_API_RULES.md](docs/AITUNNEL_API_RULES.md) — правила работы с AITunnel API.

## Лицензия

Проект разработан в рамках частной инициативы ClickJurist. Использование — по согласованию с правообладателем.
