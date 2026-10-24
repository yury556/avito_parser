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
-- источник лота (URL поиска, из которого пришёл) — заполняет парсер
CREATE TABLE IF NOT EXISTS avito_original.lot_source (
    avito_id BIGINT PRIMARY KEY,
    source_url TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
