BEGIN;

CREATE TABLE IF NOT EXISTS avito_original.notification_outbox (
    id BIGSERIAL PRIMARY KEY,
    avito_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('new', 'good')),
    reason_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    telegram_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (avito_id, kind, reason_key)
);

CREATE INDEX IF NOT EXISTS notification_outbox_ready_idx
    ON avito_original.notification_outbox (next_retry_at, id)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS notification_outbox_sending_claimed_idx
    ON avito_original.notification_outbox (claimed_at)
    WHERE status = 'sending';

COMMIT;
