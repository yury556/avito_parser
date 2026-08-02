from __future__ import annotations

import os
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.exceptions import AirflowException
from airflow.operators.python import PythonOperator

AI_ENRICH_URL = os.getenv(
    "AI_ENRICH_URL",
    "http://ai-stack-ai_enrich-1:8000/batch",
)
AI_ENRICH_LIMIT = int(os.getenv("AI_ENRICH_LIMIT", "50"))


def call_ai_enrich_batch(**context) -> int:
    payload = {"limit": AI_ENRICH_LIMIT}
    try:
        resp = requests.post(
            AI_ENRICH_URL,
            json=payload,
            timeout=600,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise AirflowException(f"ai_enrich batch call failed: {e}")

    result = resp.json()
    processed = result.get("processed", 0)
    errors = result.get("errors", 0)
    context["ti"].xcom_push(key="enrich_result", value=result)

    if errors > 0:
        context["ti"].log.warning(
            "ai_enrich batch: processed=%s, errors=%s", processed, errors
        )
    else:
        context["ti"].log.info(
            "ai_enrich batch: processed=%s, errors=%s", processed, errors
        )

    return processed


default_args = {
    "owner": "data-eng",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="midraw_to_details",
    description="Call ai_enrich /batch to enrich all unprocessed midraw.avito_ads into detail.ads.",
    default_args=default_args,
    schedule="*/30 * * * *",
    start_date=datetime(2026, 8, 1),
    catchup=False,
    max_active_runs=1,
    tags=["avito", "ai", "midraw", "details"],
) as dag:
    enrich_batch = PythonOperator(
        task_id="ai_enrich_batch",
        python_callable=call_ai_enrich_batch,
    )
