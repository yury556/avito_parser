-- ============================================================
-- Migration 002: Добавление поля description в avito_original.ads
-- Парсер теперь сохраняет полное описание объявления.
-- ============================================================

ALTER TABLE avito_original.ads
    ADD COLUMN IF NOT EXISTS description TEXT;

-- Индекс для полнотекстового поиска по описанию (опционально)
-- CREATE INDEX IF NOT EXISTS ix_ads_description_gin ON avito_original.ads
--     USING gin(to_tsvector('russian', coalesce(description, '')));