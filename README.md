# Avito Parser + Airflow + dbt

Парсер Авито с нон-стоп записью в отдельную схему Postgres `avito_original`.
Данные мониторятся в DBeaver отдельно от пайплайна Airflow.

## Архитектура

```
parser_avito/
  config.toml          ← URLs, фильтры, API ключ (gitignored)
  entrypoint.sh        ← запускает uvicorn + бесконечный цикл парсинга
  avito_service.py     ← FastAPI: /parse и /health
  src/                 ← движок парсера

airflow-avito-project/
  docker-compose.yml   ← postgres + redis + airflow + dbt + avito-parser-service
  dags/                ← DAG: parse → raw → midraw
  include/             ← pipeline helpers
  dbt/                 ← dbt модели для midraw
```

**Поток данных:**

1. `avito-parser-service` (Docker) крутит бесконечный цикл парсинга
2. В каждом цикле пишет объявления в `avito_original.ads` (Postgres)
3. Параллельно сохраняет `result/ads.json` на хост
4. Airflow DAG `avito_to_raw_midraw` раз в час грузит JSON → `raw.avito_ads_csv` → dbt midraw

## Требования

- Docker + Docker Compose
- macOS / Linux (тестировалось на macOS + zsh)
- DBeaver (опционально, для просмотра данных)

## Быстрый старт

### 1. Поднять инфра

```bash
cd airflow-avito-project
docker compose up -d
```

Контейнеры:

- `external-postgres:5433` (порт на хосте)
- `airflow-webserver:8081`
- `airflow-scheduler`
- `avito-parser-service:8001`

### 2. Настроить парсер

Отредактируйте `parser_avito/config.toml`:

- `urls` — список поисковых URL Авито
- `cookies_api_key` — ключ для antibot-сервиса
- `pause_general` — пауза между циклами (сек.)
- Фильтры: `keys_word_black_list`, `seller_black_list`, `min_price`, `max_price` ...

> `config.toml` gitignored — не коммитить в репозиторий.

### 3. Запустить парсинг (включить нон-стоп)

```bash
cd airflow-avito-project
docker compose up -d avito-parser-service
```

Парсер стартует автоматически при старте контейнера (через `entrypoint.sh`).

Проверить:

```bash
# Логи
docker logs airflow-avito-project-avito-parser-service-1 -f

# Должны увидеть === Parse cycle === и Write to Postgres: N rows

# Healthcheck
curl http://localhost:8001/health
```

### 4. Отключить парсинг (остановить)

```bash
cd airflow-avito-project
docker compose stop avito-parser-service
```

### 5. Запустить снова

```bash
cd airflow-avito-project
docker compose start avito-parser-service
```

### 6. Полная остановка всей инфры

```bash
cd airflow-avito-project
docker compose down
```

## Просмотр данных в DBeaver

- **Host:** `localhost`
- **Port:** `5433`
- **DB:** `avito_dwh`
- **User:** `avito`
- **Password:** `avito123`

Схемы:

- `avito_original` — прямые данные парсера (ваша таблица `ads`)
- `raw` — промежуточные CSV от Airflow

## Мониторинг

| Команда                                                                                                   | Что делает                                        |
| ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `docker compose ps`                                                                                            | Статус всех контейнеров               |
| `docker logs ... -f`                                                                                           | Логи парсера в реальном времени |
| `docker exec -it external-postgres-1 psql -U avito -d avito_dwh -c "SELECT COUNT(*) FROM avito_original.ads;"` | Количество строк в БД                    |
| `docker compose logs airflow-scheduler`                                                                        | Логи Airflow                                           |
| `curl http://localhost:8001/health`                                                                            | Healthcheck парсера                                 |

## Airflow

- UI: `http://localhost:8081` (login: `airflow`, password: `airflow`)
- DAG `avito_to_raw_midraw`:
  - `parse_avito_to_raw_csv` — парсит Avito и пишет в `raw.avito_ads_csv`
  - `load_ads_from_json` — читает `result/ads.json` и пишет в `raw`
  - `dbt_build_midraw` — собирает dbt модель `avito_ads`
- Schedule: каждый час в :00

## FAQ

**Парсер падал, что делать?**
Контейнер авто-перезапускается (`restart: unless-stopped`). Проверьте логи:

```bash
docker logs airflow-avito-project-avito-parser-service-1 --tail 50
```

**Avito блокирует (403/429)?**
Встроенный механизм покупки новых cookies отрабатывает автоматически.
Если блоки чаще обычного — увеличьте `pause_general` в `config.toml`.

**Потерялись cookies после рестарта контейнера?**
Смонтируйте директорию cookies в `docker-compose.yml` (опционально).

**Как изменить URLs?**
Редактируйте `parser_avito/config.toml` и перезапустите:

```bash
docker compose restart avito-parser-service
```

## Структура коммитов

- `parser_avito/avito_service.py` — HTTP handlers + Postgres writer
- `parser_avito/entrypoint.sh` — entrypoint для контейнера
- `airflow-avito-project/docker-compose.yml` — volumes, env, command для parser
- `airflow-avito-project/dags/avito_to_raw_midraw.py` — DAG с parallel задачами


## Apache Superset

Compose поднимает изолированный контур Superset: superset-metadb хранит только
метаданные BI, superset отдает UI, а superset-redis, superset-worker и
superset-beat обслуживают кэш, фоновые и периодические задачи. Ни один из
этих сервисов не использует Redis или metadata DB Airflow.

Перед первым запуском скопируй .env.example в .env и замени как минимум
SUPERSET_SECRET_KEY, SUPERSET_META_DB_PASSWORD и
SUPERSET_ADMIN_PASSWORD. Затем запусти:
docker compose up -d
superset-init однократно применит миграции и создаст администратора. UI
доступен только с локальной машины на http://127.0.0.1:18088; при занятом
порте задай другое значение SUPERSET_HOST_PORT в .env. Metadata DB и Redis
Superset не публикуют порты на хост, поэтому не конфликтуют с уже запущенными
контейнерами.

Для подключения витрины проекта в Superset добавь Database Connection со
строкой:
postgresql+psycopg2://avito:avito123@external-postgres:5432/avito_dwh
Адрес external-postgres работает только из контейнерной сети Compose; для
подключения из браузера он не нужен.
