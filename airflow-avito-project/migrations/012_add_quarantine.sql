-- ============================================================
-- Migration 012: карантин лотов (низкая уверенность Jev)
-- ============================================================

CREATE TABLE IF NOT EXISTS avito_original.quarantine (
    avito_id BIGINT PRIMARY KEY,
    reason TEXT NOT NULL,
    jev_confidence REAL,
    entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    result TEXT,
    status TEXT NOT NULL DEFAULT 'waiting'  -- waiting | done | failed
);

CREATE INDEX IF NOT EXISTS idx_quarantine_status ON avito_original.quarantine (status, entered_at);