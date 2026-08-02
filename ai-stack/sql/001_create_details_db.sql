-- Создание БД details (выполнить один раз)
-- psql -h localhost -p 5433 -U avito -c "CREATE DATABASE details;"

-- Подключаемся: \c details

CREATE SCHEMA IF NOT EXISTS detail;
CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS detail.ads (
    id                SERIAL PRIMARY KEY,
    avito_id          INTEGER NOT NULL,

    -- AI-извлечённые данные
    category          TEXT,
    brand             TEXT,
    model             TEXT,
    tags              JSONB DEFAULT '[]'::jsonb,

    -- Сырьё, которое подавали на вход
    input_title       TEXT,
    input_description TEXT,

    -- Мета-информация
    ai_model          TEXT DEFAULT 'qwen2.5:7b',
    ai_version        TEXT DEFAULT 'v1',
    ai_processed_at   TIMESTAMPTZ DEFAULT NOW(),
    ai_status         TEXT DEFAULT 'pending',  -- pending, success, error
    ai_error          TEXT,

    UNIQUE (avito_id)
);

CREATE INDEX IF NOT EXISTS idx_detail_ads_status ON detail.ads(ai_status);
CREATE INDEX IF NOT EXISTS idx_detail_ads_category ON detail.ads(category);
CREATE INDEX IF NOT EXISTS idx_detail_ads_brand ON detail.ads(brand);