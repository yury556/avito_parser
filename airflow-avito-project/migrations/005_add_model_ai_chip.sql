-- ============================================================
-- Migration 005: model_ai + chip в detail.ads
-- model  — модель после детерминированной валидации по URL-slug
-- model_ai — модель как отдал LLM (аудит, откат)
-- chip   — канонический «чип» товара (i5-13600kf, rtx 5060 ti, ddr4 32gb)
-- ============================================================

ALTER TABLE detail.ads
    ADD COLUMN IF NOT EXISTS model_ai TEXT,
    ADD COLUMN IF NOT EXISTS chip TEXT;

CREATE INDEX IF NOT EXISTS idx_detail_ads_chip ON detail.ads (chip);
