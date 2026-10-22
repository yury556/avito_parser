-- ============================================================
-- Migration 008: new_notified_at — когда лот уже показывали как «новый»
-- (отдельно от alerted_at, который про «выгодные»)
-- ============================================================

ALTER TABLE detail.ads
    ADD COLUMN IF NOT EXISTS new_notified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_detail_ads_new_notified
    ON detail.ads (new_notified_at) WHERE new_notified_at IS NULL;
