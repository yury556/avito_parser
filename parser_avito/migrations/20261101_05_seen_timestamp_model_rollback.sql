-- Только для явного отката МИГРАЦИИ после отката всех новых потребителей.
-- При обычном откате кода этот файл НЕ выполнять: старый код совместим с
-- новыми колонками и продолжает читать parsed_at.
BEGIN;

DROP INDEX IF EXISTS avito_original.ix_parser_runs_finished_at;
DROP INDEX IF EXISTS avito_original.ix_ads_last_seen_at;
DROP TABLE IF EXISTS avito_original.parser_runs;
ALTER TABLE avito_original.ads
    DROP COLUMN IF EXISTS last_price_changed_at,
    DROP COLUMN IF EXISTS content_updated_at,
    DROP COLUMN IF EXISTS last_seen_at,
    DROP COLUMN IF EXISTS first_seen_at;

COMMIT;
