-- ============================================================
-- Migration 006: метрики обогащения и технические данные лота
-- detail.ads: ai_duration_ms (длительность запроса к LLM), ai_attempts
-- avito_original.ads: published_at (дата создания объявления на Авито),
--                      views_total, views_today
-- avito_original.fetch_log: журнал запросов к странице объявления
-- ============================================================

ALTER TABLE detail.ads
    ADD COLUMN IF NOT EXISTS ai_duration_ms INTEGER,
    ADD COLUMN IF NOT EXISTS ai_attempts INTEGER;

ALTER TABLE avito_original.ads
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS views_total INTEGER,
    ADD COLUMN IF NOT EXISTS views_today INTEGER;

CREATE TABLE IF NOT EXISTS avito_original.fetch_log (
    ts timestamptz NOT NULL DEFAULT now(),
    avito_id BIGINT NOT NULL,
    method text,          -- http | browser
    kind text,            -- description | check | title
    duration_s real,
    ok boolean,
    note text
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_avito ON avito_original.fetch_log (avito_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ads_published_at ON avito_original.ads (published_at);
