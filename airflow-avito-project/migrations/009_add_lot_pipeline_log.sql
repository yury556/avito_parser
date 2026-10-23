-- ============================================================
-- Migration 009: журнал конвейера lot_pipeline_log
-- Единая хронология жизни лота: raw → midraw → enrich → карантин.
-- Append-only: одна строка на переход этапа, время = разница ts.
-- ============================================================

CREATE TABLE IF NOT EXISTS avito_original.lot_pipeline_log (
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    avito_id BIGINT NOT NULL,
    stage TEXT NOT NULL,
    duration_ms INT,
    meta JSONB
);

CREATE INDEX IF NOT EXISTS idx_pipeline_avito
    ON avito_original.lot_pipeline_log (avito_id, ts);
CREATE INDEX IF NOT EXISTS idx_pipeline_stage
    ON avito_original.lot_pipeline_log (stage, ts);