"""Дедуп viewed в Postgres: заменяет SQLite-файл (SPOF в контейнере).

Таблица avito_original.viewed(id, price, seen_at), PK (id, price).
build_viewed_handler(): Postgres, если задан POSTGRES_HOST, иначе SQLite.
"""
import os

from avito_parser.entities import Item


class PostgresViewedHandler:
    def __init__(self):
        self._conf = dict(
            host=os.environ.get("POSTGRES_HOST", "external-postgres"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "avito_dwh"),
            user=os.environ.get("POSTGRES_USER", "avito"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
        )
        self._create_table()

    def _conn(self):
        import psycopg2
        return psycopg2.connect(**self._conf)

    def _create_table(self):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS avito_original.viewed (
                    id BIGINT,
                    price NUMERIC,
                    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (id, price)
                )
                """
            )

    def add_record(self, ad: Item):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO avito_original.viewed (id, price)
                VALUES (%s, %s)
                ON CONFLICT (id, price) DO UPDATE SET seen_at = now()
                """,
                (ad.id, ad.priceDetailed.value),
            )

    def add_record_from_page(self, ads: list[Item]):
        from psycopg2.extras import execute_values
        records = [(ad.id, ad.priceDetailed.value) for ad in ads]
        with self._conn() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO avito_original.viewed (id, price)
                VALUES %s
                ON CONFLICT (id, price) DO UPDATE SET seen_at = now()
                """,
                records,
            )

    def record_exists(self, record_id, price) -> bool:
        # IS NOT DISTINCT FROM корректно сравнивает и NULL-цену
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM avito_original.viewed "
                "WHERE id = %s AND price IS NOT DISTINCT FROM %s",
                (record_id, price),
            )
            return cur.fetchone() is not None


def build_viewed_handler():
    """Postgres, если конфиг задан в env; иначе SQLite (старое поведение)."""
    if os.environ.get("POSTGRES_HOST"):
        from avito_parser.storages.viewed import PostgresViewedHandler
        return PostgresViewedHandler()
    from avito_parser.storages.sqlite import SQLiteDBHandler
    return SQLiteDBHandler()