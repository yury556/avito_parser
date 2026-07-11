from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

from avito_pipeline.config import postgres_config_from_env
from avito_pipeline.postgres_io import write_ads_as_raw_csv
from avito_pipeline.synthetic import synthetic_ads


SYNTHETIC_SOURCE_URLS = ["synthetic://avito/sample-search"]


def synthetic_to_raw(**context) -> int:
    return write_ads_as_raw_csv(
        config=postgres_config_from_env(),
        run_id=context["run_id"],
        source_urls=SYNTHETIC_SOURCE_URLS,
        ads=synthetic_ads(),
        source_system="synthetic_avito",
    )


default_args = {
    "owner": "data-eng",
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
}


with DAG(
    dag_id="synthetic_avito_to_raw_midraw",
    description="Synthetic duplicate of Avito pipeline for blocked or unavailable Avito parsing.",
    default_args=default_args,
    schedule="0 * * * *",
    start_date=datetime(2026, 7, 1),
    catchup=False,
    max_active_runs=1,
    tags=["avito", "synthetic", "postgres", "dbt", "raw", "midraw"],
) as dag:
    load_raw_csv = PythonOperator(
        task_id="generate_synthetic_raw_csv",
        python_callable=synthetic_to_raw,
    )

    build_midraw = BashOperator(
        task_id="dbt_build_midraw",
        bash_command="cd /opt/airflow/dbt && dbt run --profiles-dir . --select avito_ads && dbt test --profiles-dir . --select avito_ads",
    )

    load_raw_csv >> build_midraw

