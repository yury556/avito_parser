# AI Stack — обогащение объявлений через Ollama

## Быстрый старт

```bash
# 1. Создать БД details (один раз)
psql -h localhost -p 5433 -U avito -c "CREATE DATABASE details;"
psql -h localhost -p 5433 -U avito -d details -f sql/001_create_details_db.sql

# 2. Запустить стек
docker compose up -d

# 3. Проверить
curl http://localhost:11434/api/tags
curl http://localhost:8000/health

# 4. Обработать одно объявление
curl -X POST http://localhost:8000/enrich \
  -H "Content-Type: application/json" \
  -d '{"title":"Видеокарта Zotac GeForce RTX 5070 б/у","description":""}'
```

## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | /health | Проверка сервиса |
| POST | /enrich | Обработать одно объявление |
| POST | /batch | Обработать из БД (для Airflow) |

## Структура

```
ai-stack/
├── docker-compose.yml            ← Ollama + ai_enrich
├── .env                           ← Настройки
├── sql/
│   └── 001_create_details_db.sql ← Схема БД
└── services/
    └── ai_enrich/
        ├── Dockerfile
        ├── app.py                 ← FastAPI-сервис
        ├── requirements.txt
        └── prompts/
            └── v1.txt             ← Промпт
```