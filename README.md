# КликЮрист Production

Юридический ИИ-сервис: трёхстадийный визард (Stage 1 — анализ → Stage 2 — чек-лист → Stage 3 — документ) с анонимностью данных, веб-фактчекингом, guardrails и оплатой через Робокассу.

> **Бренд проекта:** кириллическое написание **КликЮрист** используется во всей кодовой базе, логах и документации. Латинское написание `ClickJurist` сохранено только в публичных идентификаторах репозитория/импортов, требующих ASCII (URL, package names).

> **Терминология:** в проекте используется понятие **«анонимность данных»** (ПДн до анализа заменяются на безопасные метки, IP и браузер не привязываются к переписке, история диалога живёт только в браузере). Использование слова «обезличивание» не допускается.

Полное ТЗ: [prompt.150926.md](prompt.150926.md) · [prompt160926.md](prompt160926.md) · [prompt.150926.md](prompt170926.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Архитектура за 30 секунд

```
Клиент (SPA) ─▶ POST /api/wizard/start (STAGE_1 — анализ)
                     │
              STAGE 1 (контур РФ)
              YandexGPT / Ollama / regex-маскировка
                     │
                     ▼
              STAGE 2 (чеклист действий)
              Галочки в браузере → /api/wizard/checklist
                     │
                     ▼
              STAGE 3 (документ)
              LLM-генерация → de-anonymisation
                     │
              Paywall (Робокасса)
                     │
                     ▼
              Полный документ + скачивание PDF
```

Опрос состояния активной задачи идёт через:
- `GET  /api/wizard/state`     — точка для восстановления процесса после F5
- `POST /api/wizard/checklist` — синхронизация галочек Stage 2
- `GET  /api/wizard/payment-url` — формирование ссылки на оплату Робокассы

Чат-бот — отдельный модуль `/chatbot/` (Zero-Storage, режим анонимности данных).

---

## Безопасность paywall (Stage 3)

До подтверждения оплаты реальный текст документа **никогда** не попадает в DOM:

1. `renderDocument({ markdown, isPaid })` при `isPaid === false` обрезает Markdown до первых **30 %** (шапка и описание) и замещает просительную часть случайной декоративной «рыбой» — выделить и прочитать через DevTools невозможно.
2. CSS-блюр (`.is-blurred`) остаётся как декоративный задний план под виджетом Робокассы. Это **не** механизм защиты — без замены содержимого в DOM любой пользователь смог бы прочесть документ через accessibility tree.
3. После ответа Робокассы `is_paid` поднимается в `WizardSession`, и `renderDocument` повторно вызывается с полным текстом.

Восстановление процесса после F5:

- `frontend/script.js` после монтирования делает `GET /api/wizard/state`;
- если `has_active_task: true` — UI переключается на экран лоадера и запускается `pollWizardUntilReady()`.

---

## Начало работы

1. **Клонируйте репозиторий:**
    ```bash
    git clone https://github.com/markmus11012020-max/CLICKJURIST_RANEPA.git
    cd CLICKJURIST_RANEPA
    ```

2. **Настройте окружение:**
    - Создайте файл `.env` из примера `.env.example` и заполните переменные.
    - Установите зависимости:
      ```bash
      python -m venv venv
      .\venv\Scripts\python.exe -m pip install -r requirements.txt
      .\venv\Scripts\python.exe -m pip install ruff
      ```

3. **Запустите проект:**
    ```bash
    start.bat
    ```

---

## CI и качество кода

| Шаг                          | Команда                                                                       | Цель |
| --- | --- | --- |
| Юнит- и интеграционные тесты | `python -m pytest`                                                           | 180 тестов должны быть зелёными. |
| Линтеры и типы               | `python -m ruff check .`                                                     | Без ошибок (`All checks passed!`). |
| Smoke-чек фронтенда          | `python validate_frontend.py`                                                | Проверка, что HTML/JS не разъехались после правок. |

Все три шага обязательны для PR. Конфигурация ruff хранится в `pyproject.toml`
(`[tool.ruff.lint]` и `[tool.ruff.lint.per-file-ignores]`).

---

## Структура репозитория

```
.
├── .github/workflows/           # CI/CD пайплайны
├── backend/                       # FastAPI + бизнес-логика
│   ├── api/                       # HTTP-слой, разбит по доменам
│   │   ├── deps.py                # FastAPI-зависимости: payment_required, session_gate
│   │   ├── router_wizard.py       # Визард (Stage 1/2/3 + payment integration)
│   │   └── router_payments.py     # Webhook Робокассы для Wizard-сценария (shp_session_id)
│   ├── core/                      # Ядро бизнес-инвариантов (152-ФЗ)
│   │   └── anonymizer.py          # deanonymize_document — обратная подстановка ПДн после оплаты
│   ├── services/                  # Бизнес-логика и интеграции
│   │   └── robokassa.py           # Подпись Result URL, генерация PaymentURL
│   ├── wizard_store.py            # SQLite-снимки WizardSession + холодная таблица wizard_cases
│   ├── security.py                # Извлечение IP, fingerprint
│   ├── main.py                    # Сборка приложения, middleware, статика
│   ├── config.py                  # Настройки (env + KMS bootstrap)
│   ├── models.py                  # Pydantic-схемы (WizardStateResponse, и т.д.)
│   └── ...
├── chatbot/                       # Опциональный модуль чат-бота (ES-модули)
│   ├── api/                       # /api/chatbot (greeting, message, history)
│   ├── domain/                    # Entities, enums, interfaces (с str-Enum)
│   ├── knowledge/                 # База знаний: catalog, safety, history
│   ├── services/                  # Orchestrator, intent_resolver, sanitizer
│   ├── static/js/                 # Виджет (Composer, ChatPanel, MessageList, …)
│   └── llm/                       # LLMGateway + streaming + rate-limit
├── data/                          # Данные для проекта
├── deploy/                        # Скрипты для деплоя (Yandex Cloud)
├── docs/                          # Документация проекта
├── frontend/                      # Frontend-приложение (SPA, vanilla JS)
│   ├── script.js                  # Точка входа: bind() + bindWizard() + recovery
│   ├── js/app.js                  # Биндинги формы консультации
│   ├── js/features/wizard.js      # Визард + защита renderDocument (Stage 3)
│   ├── js/ui/state.js             # wizardState + pollWizardUntilReady
│   ├── js/ui/paywall.js           # Окно оплаты Робокассы
│   └── style.css                  # Темы + .wizard-paper #fcfbfa + stepper 44px на mobile
├── prompts/                       # Промпты для LLM
├── secrets/                       # Секреты для проекта
├── tests/                         # Pytest (180 тестов, оффлайн-режим)
│   ├── test_chatbot.py            # Orchestrator + privacy article
│   ├── test_chatbot_api.py        # HTTP-слой /api/chatbot
│   ├── test_chatbot_widget.py     # Composer subscribe, state-binding
│   ├── test_chatbot_stream.py     # Стриминг из LLMGateway
│   ├── test_providers.py          # LLM-провайдеры + chain order
│   ├── test_anonymizer.py         # deanonymize_document: плейсхолдеры, best-effort, формат карты
│   ├── test_payments_webhook.py   # Webhook Робокассы: подпись, shp_session_id, идемпотентность
│   ├── test_wizard_paywall.py     # Paywall STAGE_3: превью 30%, размаскирование после оплаты
│   └── ...
├── .clinerules                    # Правила для Cline
├── .env.example                   # Пример файла с переменными окружения
├── pyproject.toml                 # Конфигурация ruff + pytest
├── README.md                      # Этот файл
└── start.bat                      # Скрипт для запуска проекта
```

---

## Документация

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — поток данных и схема БД.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — запуск, переменные, мониторинг.
- [docs/SECURITY.md](docs/SECURITY.md) — соответствие 152-ФЗ и анонимность данных.
- [docs/ROADMAP.md](docs/ROADMAP.md) — план развития.
- [deploy/yandex-cloud/README.md](deploy/yandex-cloud/README.md) — production-развёртывание.
- [docs/AITUNNEL_API_RULES.md](docs/AITUNNEL_API_RULES.md) — правила работы с AITunnel API.

---

## Лицензия

Проект разработан в рамках частной инициативы КликЮрист. Использование — по согласованию с правообладателем.