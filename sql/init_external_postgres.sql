CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS midraw;

CREATE TABLE IF NOT EXISTS raw.avito_ads_csv (
    run_id text NOT NULL,
    row_number integer NOT NULL,
    source_url text NOT NULL,
    csv_header text NOT NULL,
    csv_row text NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, row_number)
);

CREATE TABLE IF NOT EXISTS raw.avito_load_audit (
    run_id text PRIMARY KEY,
    source_system text NOT NULL,
    source_url_count integer NOT NULL,
    row_count integer NOT NULL,
    status text NOT NULL,
    message text,
    loaded_at timestamptz NOT NULL DEFAULT now()
);

