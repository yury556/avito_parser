from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values

from avito_pipeline.config import PostgresConfig
from avito_pipeline.csv_contract import AvitoAd, ad_to_csv_row, csv_header


@contextmanager
def postgres_connection(config: PostgresConfig):
    conn = psycopg2.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_raw_objects(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS raw")
        cur.execute("CREATE SCHEMA IF NOT EXISTS midraw")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS raw.avito_ads_csv (
                run_id text NOT NULL,
                row_number integer NOT NULL,
                source_url text NOT NULL,
                csv_header text NOT NULL,
                csv_row text NOT NULL,
                loaded_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (run_id, row_number)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS raw.dag_run_history (
                run_id       text      NOT NULL,
                avito_id     bigint    NOT NULL,
                row_number   integer   NOT NULL,
                loaded_at    timestamptz NOT NULL DEFAULT now(),
                source_system text     NOT NULL,
                PRIMARY KEY (run_id, avito_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS raw.avito_load_audit (
                run_id text PRIMARY KEY,
                source_system text NOT NULL,
                source_url_count integer NOT NULL,
                row_count integer NOT NULL,
                status text NOT NULL,
                message text,
                loaded_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )


def write_ads_as_raw_csv(
    config: PostgresConfig,
    run_id: str,
    source_urls: list[str],
    ads: list[AvitoAd],
    source_system: str,
) -> int:
    with postgres_connection(config) as conn:
        ensure_raw_objects(conn)
        header = csv_header()
        rows = [
            (run_id, index, ";".join(source_urls), header, ad_to_csv_row(ad))
            for index, ad in enumerate(ads, start=1)
        ]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM raw.avito_ads_csv WHERE run_id = %s", (run_id,))
            if rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO raw.avito_ads_csv
                        (run_id, row_number, source_url, csv_header, csv_row)
                    VALUES %s
                    """,
                    rows,
                )
                dag_history_rows = [
                    (run_id, ad.avito_id, index, source_system)
                    for index, ad in enumerate(ads, start=1)
                ]
                execute_values(
                    cur,
                    """
                    INSERT INTO raw.dag_run_history
                        (run_id, avito_id, row_number, source_system)
                    VALUES %s
                    ON CONFLICT (run_id, avito_id) DO NOTHING
                    """,
                    dag_history_rows,
                )
            cur.execute(
                """
                INSERT INTO raw.avito_load_audit
                    (run_id, source_system, source_url_count, row_count, status, message)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    source_system = EXCLUDED.source_system,
                    source_url_count = EXCLUDED.source_url_count,
                    row_count = EXCLUDED.row_count,
                    status = EXCLUDED.status,
                    message = EXCLUDED.message,
                    loaded_at = now()
                """,
                (run_id, source_system, len(source_urls), len(rows), "success", None),
            )
    return len(rows)


def fetch_ads_from_original(config: PostgresConfig, since: datetime) -> list[AvitoAd]:
    with postgres_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT avito_id, title, price_rub, url, location, seller, parsed_at
                FROM avito_original.ads
                WHERE parsed_at > %s
                ORDER BY parsed_at
                """,
                (since,),
            )
            return [
                AvitoAd(
                    avito_id=row[0],
                    title=row[1] or "",
                    price_rub=row[2],
                    url=row[3] or "",
                    location=row[4],
                    seller=row[5],
                    parsed_at=row[6],
                )
                for row in cur.fetchall()
            ]

