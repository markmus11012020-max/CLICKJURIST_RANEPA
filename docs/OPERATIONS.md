# ClickJurist Production — Эксплуатация

## Локальный запуск

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
cp .env.example .env
python -m backend.main
```

API: http://localhost:8000, документация: http://localhost:8000/api/docs.

## Переменные окружения (см. .env.example)

12 разделов:
1. Приложение; 2. 152-ФЗ; 3. Хранилище; 4. Stage 1 (маскировка);
5. Stage 2 (AITunnel); 6. Failover; 7. Web-фактчекинг;
8. Yandex KMS; 9. Zero-Storage Logging; 10. Robokassa; 11. Тарифы; 12. PDF.

## Production (Yandex Serverless Containers)

1. Создать контейнер `cr.yandex/<folder>/clickjurist:tag`.
2. Зашифровать секреты: `yc kms symmetric-crypto encrypt --key-id ... --plaintext-file .env.production --ciphertext-file secrets/kms-secrets.b64`.
3. В контейнере: `APP_ENV=production`, `DATABASE_URL=postgresql://...`,
   `YANDEX_KMS_ENABLED=true`, `YANDEX_KMS_CIPHERTEXT_FILE=secrets/kms-secrets.b64`.
4. Точка входа: `uvicorn backend.main:app --host 0.0.0.0 --port 8080`.

## Тесты

```bash
pytest -q
```

Покрывают: безопасность, маскировку ПДн, мегапайплайн в оффлайн-режиме, подписи Robokassa.

## Логи

* stdout — структурированный JSON, проходит через `PIIRedactingFilter`;
* Yandex Cloud Logging — асинхронный shipper, батчи по 100 записей.

## Мониторинг

* `/api/health` — состояние контуров (masking, analysis, web_search, kms).
* `/api/stats` — анонимная агрегация (sessions, payments, requests_total).
