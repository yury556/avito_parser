-- ClickHouse init script
-- Создаёт БД mart, bridge-таблицы (PostgreSQL engine), target-таблицы витрин
-- и Refreshable Materialized Views, которые автоматически обновляют витрины

CREATE DATABASE IF NOT EXISTS mart;

-- ============================================================
-- 1. Bridge-таблицы (PostgreSQL engine, данных в CH не хранят)
--    Это «окна» в PostgreSQL — для чтения данных на лету
-- ============================================================

CREATE TABLE IF NOT EXISTS mart.pg_midraw_ads
(
    avito_id       UInt64,
    title          String,
    price_rub      Decimal(12, 2),
    url            String,
    location       String,
    seller         String,
    description    String,
    parsed_at      DateTime,
    source_system  String
)
ENGINE = PostgreSQL(
    'external-postgres:5432',
    'avito_dwh',
    'avito_ads',
    'avito',
    'avito123',
    'midraw'
);

CREATE TABLE IF NOT EXISTS mart.pg_detail_ads
(
    avito_id         UInt64,
    category         String,
    brand            String,
    model            String,
    tags             String,
    input_title      String,
    price_rub        Decimal(12, 2),
    ai_status        String,
    ai_processed_at  DateTime
)
ENGINE = PostgreSQL(
    'external-postgres:5432',
    'avito_dwh',
    'ads',
    'avito',
    'avito123',
    'detail'
);

-- ============================================================
-- 2. Target-таблицы витрин (хранят данные в ClickHouse)
-- ============================================================

-- Детализированная: фильтр по категории, цена, теги, ссылка
CREATE TABLE IF NOT EXISTS mart.avito_ads_detail
(
    avito_id       UInt64,
    category       String,
    brand          String,
    model          String,
    title          String,
    url            String,
    price_rub      Decimal(12, 2),
    tags           Array(String),
    ai_status      String,
    ai_processed_at DateTime,
    updated_at     DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY category
ORDER BY (category, model, avito_id);

-- Агрегированная по модели: средняя/медианная цена
CREATE TABLE IF NOT EXISTS mart.avito_model_stats
(
    category      String,
    model         String,
    count_ads     UInt64,
    avg_price     Decimal(12, 2),
    median_price  Decimal(12, 2),
    min_price     Decimal(12, 2),
    max_price     Decimal(12, 2),
    top_tags      Array(String) DEFAULT [],
    updated_at    DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY category
ORDER BY (category, model);

-- Агрегированная по категории: средняя/медианная цена
CREATE TABLE IF NOT EXISTS mart.avito_category_stats
(
    category      String,
    count_ads     UInt64,
    avg_price     Decimal(12, 2),
    median_price  Decimal(12, 2),
    min_price     Decimal(12, 2),
    max_price     Decimal(12, 2),
    updated_at    DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY category
ORDER BY (category);

-- ============================================================
-- 3. Refreshable Materialized Views
--    Автоматически перезапрашивают данные из PG каждые 15 минут
-- ============================================================

-- Витрина объявлений: джойн midraw (title, url) + detail (category, tags, price)
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.ads_detail_mv
REFRESH EVERY 15 MINUTE
TO mart.avito_ads_detail
AS SELECT
    d.avito_id,
    d.category,
    d.brand,
    d.model,
    m.title,
    m.url,
    d.price_rub,
    JSONExtract(d.tags, 'Array(String)') AS tags,
    d.ai_status,
    d.ai_processed_at,
    now() AS updated_at
FROM mart.pg_detail_ads AS d
LEFT JOIN mart.pg_midraw_ads AS m ON m.avito_id = d.avito_id
WHERE d.ai_status = 'success'
  AND d.category != '';

-- Агрегаты по модели
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.model_stats_mv
REFRESH EVERY 15 MINUTE
TO mart.avito_model_stats
AS SELECT
    category,
    model,
    count() AS count_ads,
    round(avg(price_rub), 2) AS avg_price,
    round(quantile(0.5)(price_rub), 2) AS median_price,
    min(price_rub) AS min_price,
    max(price_rub) AS max_price,
    arrayDistinct(arrayFlatten(groupArray(tags))) AS top_tags,
    now() AS updated_at
FROM mart.avito_ads_detail
WHERE model != ''
GROUP BY category, model;

-- Агрегаты по категории
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.category_stats_mv
REFRESH EVERY 15 MINUTE
TO mart.avito_category_stats
AS SELECT
    category,
    count() AS count_ads,
    round(avg(price_rub), 2) AS avg_price,
    round(quantile(0.5)(price_rub), 2) AS median_price,
    min(price_rub) AS min_price,
    max(price_rub) AS max_price,
    now() AS updated_at
FROM mart.avito_ads_detail
GROUP BY category;