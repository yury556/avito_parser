from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

from avito_pipeline.avito_parser import parse_avito_search, load_ads_from_json_file
from avito_pipeline.config import avito_config_from_env, postgres_config_from_env
from avito_pipeline.postgres_io import write_ads_as_raw_csv


def parse_avito_to_raw(**context) -> int:
    avito_config = avito_config_from_env()
    postgres_config = postgres_config_from_env()
    run_id = context["run_id"]
    ads = parse_avito_search(avito_config)
    return write_ads_as_raw_csv(
        config=postgres_config,
        run_id=run_id,
        source_urls=avito_config.urls,
        ads=ads,
        source_system="avito",
    )


def load_json_to_raw(**context) -> int:
    postgres_config = postgres_config_from_env()
    run_id = context["run_id"] + "__json"
    ads = load_ads_from_json_file()
    if not ads:
        return 0
    return write_ads_as_raw_csv(
        config=postgres_config,
        run_id=run_id,
        source_urls=[ad.url for ad in ads],
        ads=ads,
        source_system="avito-json",
    )


default_args = {
    "owner": "data-eng",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


with DAG(
    dag_id="avito_to_raw_midraw",
    description="Parse Avito hourly, store CSV rows in raw Postgres, then build dbt midraw.",
    default_args=default_args,
    schedule="0 * * * *",
    start_date=datetime(2026, 7, 1),
    catchup=False,
    max_active_runs=1,
    tags=["avito", "postgres", "dbt", "raw", "midraw"],
) as dag:
    load_raw_csv = PythonOperator(
        task_id="parse_avito_to_raw_csv",
        python_callable=parse_avito_to_raw,
    )

    load_json_csv = PythonOperator(
        task_id="load_ads_from_json",
        python_callable=load_json_to_raw,
    )

    build_midraw = BashOperator(
        task_id="dbt_build_midraw",
        bash_command="cd /opt/airflow/dbt && dbt run --profiles-dir . --select avito_ads && dbt test --profiles-dir . --select avito_ads",
    )

    [load_raw_csv, load_json_csv] >> build_midraw

