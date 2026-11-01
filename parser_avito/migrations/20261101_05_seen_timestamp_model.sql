BEGIN;

ALTER TABLE avito_original.ads
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS content_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_price_changed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS avito_original.parser_runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    urls_count INTEGER,
    found_count INTEGER,
    written_count INTEGER,
    error TEXT,
    parser_version TEXT
);

CREATE INDEX IF NOT EXISTS ix_ads_last_seen_at
    ON avito_original.ads (last_seen_at);
CREATE INDEX IF NOT EXISTS ix_parser_runs_finished_at
    ON avito_original.parser_runs (finished_at);

-- parsed_at остаётся legacy/content timestamp. Для старых строк это лучшая
-- доступная метка изменения контента; last_price_changed_at не выдумываем.
WITH history AS (
    SELECT avito_id, min(parsed_at) AS first_seen, max(parsed_at) AS last_seen
    FROM midraw.avito_ads
    GROUP BY avito_id
)
UPDATE avito_original.ads AS a
SET first_seen_at = COALESCE(a.first_seen_at, h.first_seen, a.parsed_at),
    last_seen_at = COALESCE(a.last_seen_at, h.last_seen, a.parsed_at),
    content_updated_at = COALESCE(a.content_updated_at, a.parsed_at)
FROM history AS h
WHERE h.avito_id = a.avito_id
  AND (a.first_seen_at IS NULL OR a.last_seen_at IS NULL OR a.content_updated_at IS NULL);

UPDATE avito_original.ads AS a
SET first_seen_at = COALESCE(a.first_seen_at, a.parsed_at),
    last_seen_at = COALESCE(a.last_seen_at, a.parsed_at),
    content_updated_at = COALESCE(a.content_updated_at, a.parsed_at)
WHERE NOT EXISTS (SELECT 1 FROM midraw.avito_ads AS m WHERE m.avito_id = a.avito_id)
  AND (a.first_seen_at IS NULL OR a.last_seen_at IS NULL OR a.content_updated_at IS NULL);

COMMENT ON COLUMN avito_original.ads.last_price_changed_at IS
    'NULL для старых лотов: точное время исторического изменения цены недоступно.';

COMMIT;
