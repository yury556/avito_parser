BEGIN;

CREATE TABLE IF NOT EXISTS avito_original.image_jobs (
    id BIGSERIAL PRIMARY KEY,
    avito_id BIGINT NOT NULL,
    original_url TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    job_type TEXT NOT NULL DEFAULT 'image' CHECK (job_type IN ('image', 'detail')),
    status TEXT NOT NULL DEFAULT 'retry'
        CHECK (status IN ('success', 'permanent_failure', 'retry')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    next_retry_at TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (avito_id, original_url)
);

CREATE INDEX IF NOT EXISTS image_jobs_ready_idx
    ON avito_original.image_jobs (next_retry_at, avito_id, position, id)
    WHERE status = 'retry';
CREATE INDEX IF NOT EXISTS image_jobs_ad_idx
    ON avito_original.image_jobs (avito_id, position, id);

-- Существующие лоты без исходных URL получают durable-задание на detail page.
INSERT INTO avito_original.image_jobs (avito_id, original_url, position, job_type)
SELECT avito_id, url, 0, 'detail'
FROM avito_original.ads
WHERE (image_keys IS NULL OR image_keys = '[]'::jsonb)
  AND url IS NOT NULL
  AND url <> ''
ON CONFLICT (avito_id, original_url) DO NOTHING;

COMMIT;
