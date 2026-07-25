from __future__ import annotations

from datetime import datetime, timedelta, timezone

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

from avito_pipeline.config import postgres_config_from_env
from avito_pipeline.postgres_io import write_ads_as_raw_csv, fetch_ads_from_original


def fetch_from_original(**context) -> int:
    pg_config = postgres_config_from_env()
    since = context.get("data_interval_start")
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=1)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    ads = fetch_ads_from_original(pg_config, since)
    if not ads:
        return 0
    return write_ads_as_raw_csv(
        config=pg_config,
        run_id=context["run_id"],
        source_urls=["avito_original.ads"],
        ads=ads,
        source_system="avito",
    )


default_args = {
    "owner": "data-eng",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


with DAG(
    dag_id="avito_to_raw_midraw",
    description="Read Avito ads from avito_original.ads into raw CSV, then build dbt midraw.",
    default_args=default_args,
    schedule="0 * * * *",
    start_date=datetime(2026, 7, 1),
    catchup=False,
    max_active_runs=1,
    tags=["avito", "postgres", "dbt", "raw", "midraw"],
) as dag:
    fetch_raw = PythonOperator(
        task_id="fetch_raw_from_original",
        python_callable=fetch_from_original,
    )

    build_midraw = BashOperator(
        task_id="dbt_build_midraw",
        bash_command="cd /opt/airflow/dbt && dbt run --profiles-dir . --select avito_ads && dbt test --profiles-dir . --select avito_ads",
    )

    fetch_raw >> build_midraw