-- ============================================================
-- Migration 007: alerted_price — цена на момент последнего алерта
-- Позволяет присылать лот повторно, если цена заметно изменилась
-- (раньше alerted_at ставился навсегда и лот больше не приходил).
-- ============================================================

ALTER TABLE detail.ads
    ADD COLUMN IF NOT EXISTS alerted_price NUMERIC(12, 2);

UPDATE detail.ads
SET alerted_price = price_rub
WHERE alerted_at IS NOT NULL AND alerted_price IS NULL AND price_rub > 0;
