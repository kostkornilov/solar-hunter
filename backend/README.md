# SolarHunter Backend

FastAPI сервис для оценки потенциала солнечной станции по точке:
- сбор фичей из GEE/CDS/NASA;
- инференс CatBoost (`.cbm`);
- расчет `PP`.
- локальные vendored-парсеры лежат в `backend/parsers`.

## Локальный запуск

1. Скопируйте `.env.example` в `.env` и заполните ключи (`CDS_API_KEY`, `EARTHDATA_TOKEN`).
2. Установите зависимости:
   - `pip install -r requirements.txt`
3. Запустите:
   - `python run.py`
python -m dotenv -f .env run -- python run.py

API:
- `GET /health`
- `POST /v1/evaluate`

Пример тела запроса:

```json
{
  "lat": 55.7558,
  "lon": 37.6176,
  "P": 120,
  "tariff": 8.5
}
```

