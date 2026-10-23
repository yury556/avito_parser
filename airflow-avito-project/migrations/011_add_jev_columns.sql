-- ============================================================
-- Migration 011: колонки Jev-разметки в detail.ads
-- ============================================================

ALTER TABLE detail.ads ADD COLUMN IF NOT EXISTS condition TEXT;
ALTER TABLE detail.ads ADD COLUMN IF NOT EXISTS tags_universal TEXT[];
ALTER TABLE detail.ads ADD COLUMN IF NOT EXISTS jev_confidence REAL;
ALTER TABLE detail.ads ADD COLUMN IF NOT EXISTS tier TEXT;
CREATE INDEX IF NOT EXISTS idx_ads_tier ON detail.ads (tier);