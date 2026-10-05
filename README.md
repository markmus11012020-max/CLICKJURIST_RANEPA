# ClickJurist Production

Юридический ИИ-сервис: двухэтапный мегапайплайн (LLM-1 → LLM-2) с анонимностью данных (маскировка ПДн в изолированном контуре РФ), веб-фактчекингом, guardrails и оплатой через Робокассу.

Полное ТЗ: [prompt.150926.md](prompt.150926.md) · [prompt160926.md](prompt160926.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Архитектура за 30 секунд

```
Клиент (SPA) ─▶ POST /api/query/async (202 Accepted + task_id)
                     │
        ┌─────────────┼──────────────────────────┐
        ▼             ▼                          ▼
 JWT-cookie    STAGE 1 (контур РФ)        Web-фактчекинг (агент)
 + Paywall     YandexGPT/Ollama/regex      Yandex/Serper/Tavily
        │             │                          │ приоритет consultant.ru
        └─────────────┴────────────┬─────────────┘
                                   ▼
                        STAGE 2 (внешний)
                        AITunnel: LLM-1 → LLM-2
                                   │
                                   ▼
                        Guardrails → Дисклеймер → клиенту

Клиент опрашивает статус:
    GET /api/query/status/{task_id}     — polling
    GET /api/query/stream/{task_id}     — SSE-стриминг событий
```

---
## Начало работы

1.  **Клонируйте репозиторий:**
    ```bash
    git clone https://github.com/markmus11012020-max/CLICKJURIST_RANEPA.git
    cd CLICKJURIST_RANEPA
    ```

2.  **Настройте окружение:**
    - Создайте файл `.env` из примера `.env.example` и заполните необходимые переменные.
    - Установите зависимости:
      ```bash
      pip install -r requirements.txt
      ```

3.  **Запустите проект:**
    Выполните скрипт `start.bat` для запуска всех сервисов.
    ```bash
    start.bat
    ```

## Структура репозитория

```
.
├── .github/workflows/           # CI/CD пайплайны
├── backend/                       # FastAPI + бизнес-логика
│   ├── api/                       # HTTP-слой, разбит по доменам
│   ├── services/                  # Бизнес-логика и интеграции
│   ├── main.py                    # Сборка приложения: middleware, роутеры, статика
│   ├── config.py                  # Настройки (env + KMS bootstrap)
│   ├── models.py                  # Pydantic-схемы запросов/ответов
│   └── ...
├── chatbot/                       # Опциональный модуль чат-бота
├── data/                          # Данные для проекта
├── deploy/                        # Скрипты для деплоя
├── docs/                          # Документация проекта
├── frontend/                      # Frontend-приложение (SPA)
├── prompts/                       # Промпты для LLM
├── secrets/                       # Секреты для проекта
├── tests/                         # Тесты
├── .clinerules                    # Правила для Cline
├── .env.example                   # Пример файла с переменными окружения
├── .gitignore                     # Файлы, которые не должны быть в репозитории
├── pyproject.toml                 # Конфигурация проекта
├── README.md                      # Этот файл
└── start.bat                      # Скрипт для запуска проекта
```

---

## Документация

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — поток данных и схема БД.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — запуск, переменные, мониторинг.
- [docs/SECURITY.md](docs/SECURITY.md) — соответствие 152-ФЗ.
- [docs/ROADMAP.md](docs/ROADMAP.md) — план развития.
- [deploy/yandex-cloud/README.md](deploy/yandex-cloud/README.md) — production-развёртывание.
- [docs/AITUNNEL_API_RULES.md](docs/AITUNNEL_API_RULES.md) — правила работы с AITunnel API.

---

## Лицензия

Проект разработан в рамках частной инициативы ClickJurist. Использование — по согласованию с правообладателем.
