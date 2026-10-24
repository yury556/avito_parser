-- ============================================================
-- Migration 013: справочник именованных поисков
-- Создаётся миграцией (а не кодом парсера), чтобы таблица
-- гарантированно существовала. Наполняется из config.toml
-- секции [url_names]; скан алертов от неё НЕ зависит
-- (имя ищется в Python с fallback на config).
-- ============================================================

CREATE TABLE IF NOT EXISTS avito_original.search_urls (
    url TEXT PRIMARY KEY,
    name TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);