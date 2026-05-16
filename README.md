# SolarHunter

MVP веб-приложение для оценки потенциала солнечной станции:
- интерактивная карта и ввод координат;
- online сбор фичей из GEE/CDS/NASA;
- инференс CatBoost;
- расчет срока окупаемости (PP).

## Структура

- `backend/` — FastAPI сервис `POST /v1/evaluate`.
- `frontend/` — статический интерфейс (карта + форма + таблица результата).
- `.github/workflows/ci.yml` — базовые проверки.

## Локальный запуск

1. Заполните `backend/.env` (можно скопировать из `backend/.env.example`).
2. Запуск backend:
   - `cd backend`
   - `pip install -r requirements.txt`
   - `python run.py`
3. Запуск frontend:
   - открыть `frontend/index.html` через локальный веб-сервер (`python -m http.server 8080` в папке `frontend`).

## Формулы

- `CAPEX = 75000 * P`
- `OPEX_year = 1000 * P`
- `REVENUE_year = CF * 8760 * P * tariff`
- `PP = CAPEX / (REVENUE_year - OPEX_year)`

Если `REVENUE_year <= OPEX_year`, PP возвращается как `null` с пояснением.

