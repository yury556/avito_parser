-- Apply outside a transaction: CREATE INDEX CONCURRENTLY cannot run in one.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_midraw_avito_parsed
ON midraw.avito_ads (avito_id, parsed_at DESC);
