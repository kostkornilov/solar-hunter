# SolarHunter Frontend

Статический интерфейс (карта, форма параметров, вывод результата).

## Локальный запуск

1. Запустите backend на `http://localhost:8000`.
2. Проверьте `config.js` (поле `window.SOLARHUNTER_API_BASE_URL`).
3. Поднимите статический сервер:
   - `python -m http.server 8080`
4. Откройте `http://localhost:8080`.

## Деплой в Yandex Object Storage

1. Скопируйте файлы `index.html`, `styles.css`, `app.js`, `config.js` в бакет.
2. В `config.js` укажите URL backend (через API Gateway/домен).
3. Включите static website hosting у бакета и настройте CORS на backend.

