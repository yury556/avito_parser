-- ============================================================
-- Migration 001: Оптимизация типов данных и индексов
-- Применять вручную после деплоя кода (UPSERT с WHERE, int).
-- ============================================================

-- 1. Индекс для быстрого чтения DAG-ом (WHERE parsed_at > %s)
CREATE INDEX IF NOT EXISTS ix_ads_parsed_at ON avito_original.ads (parsed_at);

-- 2. Оптимизация типов колонок
ALTER TABLE avito_original.ads
    ALTER COLUMN location  TYPE varchar(64),
    ALTER COLUMN seller    TYPE varchar(64),
    ALTER COLUMN price_rub TYPE integer;

-- 3. Одноразовая чистка dead tuples + обновление статистики
VACUUM (ANALYZE, FULL) avito_original.ads;
VACUUM (ANALYZE) raw.avito_ads_csv;
VACUUM (ANALYZE) midraw.avito_ads;