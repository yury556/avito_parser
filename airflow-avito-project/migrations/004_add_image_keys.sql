-- ============================================================
-- Migration 004: Добавление image_keys в avito_original.ads
-- Хранит метаданные скачанных изображений (MinIO ключи, URL, размер).
-- ============================================================

ALTER TABLE avito_original.ads
    ADD COLUMN IF NOT EXISTS image_keys JSONB DEFAULT '[]'::jsonb;