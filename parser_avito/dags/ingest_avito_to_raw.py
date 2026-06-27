"""
Переносит данные из БД avito в raw_dwh.
Запускается раз в час.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
import psycopg2.extras

default_args = {
    "owner": "avito",
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
}

def ingest() -> None:
    src = PostgresHook(postgres_conn_id="avito_postgres")
    dst = PostgresHook(postgres_conn_id="raw_dwh")

    # Создаём таблицы в raw_dwh, если нет
    for ddl in [
        """
        CREATE TABLE IF NOT EXISTS raw.ads (
            avito_id BIGINT PRIMARY KEY,
            url TEXT,
            title TEXT,
            price INTEGER,
            category TEXT,
            seller TEXT,
            location TEXT,
            description TEXT,
            total_views INTEGER,
            today_views INTEGER,
            is_promoted BOOLEAN,
            phone TEXT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            is_active BOOLEAN,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS raw.price_history (
            id SERIAL PRIMARY KEY,
            avito_id BIGINT,
            price INTEGER,
            detected_at TIMESTAMP
        );
        """,
    ]:
        dst.run(ddl)

    # Чистим сырой слой перед загрузкой
    dst.run("TRUNCATE TABLE raw.ads")
    dst.run("TRUNCATE TABLE raw.price_history")

    # Копируем таблицы
    for table in ("ads", "price_history"):
        # Получаем имена колонок (исключая ingested_at — она с DEFAULT)
        src_columns = [
            row[0]
            for row in src.get_records(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema='public' AND table_name='{table}' "
                f"ORDER BY ordinal_position"
            )
        ]
        cols_str = ", ".join(src_columns)

        rows = src.get_records(f"SELECT {cols_str} FROM public.{table}")
        if not rows:
            print(f"  {table}: нет данных, пропускаю")
            continue

        dst_conn = dst.get_conn()
        with dst_conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO raw.{table} ({cols_str}) VALUES %s",
                [tuple(r) for r in rows],
                page_size=500,
            )
        dst_conn.commit()
        print(f"  ✅ {table}: {len(rows)} строк перенесено")


with DAG(
    dag_id="ingest_avito_to_raw",
    default_args=default_args,
    description="Переливка из avito → raw_dwh",
    schedule="0 * * * *",
    start_date=datetime(2026, 6, 25),
    catchup=False,
    tags=["avito", "dwh"],
) as dag:

    PythonOperator(
        task_id="ingest_to_raw",
        python_callable=ingest,
    )