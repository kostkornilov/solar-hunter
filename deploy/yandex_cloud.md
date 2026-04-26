# Yandex Cloud deployment guide

## Backend (Serverless Containers)

1. Соберите контейнер:
   - `docker build -t solarhunter-backend:latest ./backend`
2. Запушьте образ в Yandex Container Registry.
3. Создайте Serverless Container с env переменными:
   - `CDS_API_KEY`
   - `EARTHDATA_TOKEN`
   - `GEE_PROJECT`
   - `MODEL_ARTIFACTS_DIR` (или заранее положите артефакты в образ)
4. Откройте endpoint через API Gateway.

## Frontend (Object Storage)

1. Обновите `frontend/config.js` на URL API Gateway.
2. Залейте статику в бакет Object Storage.
3. Включите static website hosting.

## Минимальный smoke-checklist

1. `GET /health` возвращает 200.
2. `POST /v1/evaluate` с валидными координатами возвращает `cf`, `payback_years`/`null`, `request_id`.
3. Одновременные 2-3 запроса не дают лавинообразных ошибок по внешним API.
4. В логах есть `request_id`, начало и конец обработки.

