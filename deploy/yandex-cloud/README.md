# Развёртывание в Yandex Cloud (Serverless Containers)

## Шаг 1. Сборка образа

```bash
docker build -t cr.yandex/<FOLDER>/clickjurist:v2.0.0 -f deploy/yandex-cloud/Dockerfile .
docker push cr.yandex/<FOLDER>/clickjurist:v2.0.0
```

## Шаг 2. Шифрование секретов

```bash
yc kms symmetric-crypto encrypt \
  --key-id <KMS_KEY_ID> \
  --plaintext-file .env.production \
  --ciphertext-file secrets/kms-secrets.b64
```

## Шаг 3. Запуск контейнера

- Переменные окружения: `APP_ENV=production`, `DATABASE_URL=postgresql://...`, `YANDEX_KMS_ENABLED=true`, `YANDEX_KMS_CIPHERTEXT_FILE=secrets/kms-secrets.b64`.
- Смонтировать `secrets/kms-secrets.b64` в `/app/secrets/kms-secrets.b64`.
- Порт: 8080 → маппинг в `container registry` или `serverless containers`.
