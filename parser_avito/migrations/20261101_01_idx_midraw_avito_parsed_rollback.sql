-- Apply outside a transaction: DROP INDEX CONCURRENTLY cannot run in one.
DROP INDEX CONCURRENTLY IF EXISTS midraw.idx_midraw_avito_parsed;
