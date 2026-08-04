-- ============================================================
-- Migration 003: Добавление price_rub в detail.ads
-- Пробрасываем цену из midraw.avito_ads в детальный слой.
-- ============================================================

ALTER TABLE detail.ads
    ADD COLUMN IF NOT EXISTS price_rub NUMERIC(12, 2);