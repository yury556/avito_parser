from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.exceptions import AirflowException
from airflow.operators.python import PythonOperator

from tg_alerts import dag_failure_callback


def _update_ad_state():
    """Пересчёт состояния: активные = видели в выдаче за 48 часов."""
    import psycopg2
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "external-postgres"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "avito_dwh"),
        user=os.getenv("POSTGRES_USER", "avito"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE detail.ads d SET state='активно' WHERE d.avito_id IN "
                "(SELECT DISTINCT avito_id FROM midraw.avito_ads "
                " WHERE parsed_at > now() - interval '24 hours') "
                "AND NOT (d.last_checked_at > now() - interval '24 hours')"
            )
            cur.execute(
                "UPDATE detail.ads d SET state='закрыто' WHERE d.avito_id NOT IN "
                "(SELECT DISTINCT avito_id FROM midraw.avito_ads "
                " WHERE parsed_at > now() - interval '24 hours') "
                "AND NOT (d.last_checked_at > now() - interval '24 hours')"
            )
        cur = None
        logger = logging.getLogger("airflow.task")
        logger.info("state пересчитан")
    finally:
        conn.close()

AI_ENRICH_URL = os.getenv(
    "AI_ENRICH_URL",
    "http://ai-stack-ai_enrich-1:8000/batch",
)
AI_ENRICH_LIMIT = int(os.getenv("AI_ENRICH_LIMIT", "50"))
QUARANTINE_LIMIT = int(os.getenv("QUARANTINE_LIMIT", "20"))


def call_ai_enrich_batch(**context) -> int:
    payload = {"limit": AI_ENRICH_LIMIT}
    try:
        resp = requests.post(
            AI_ENRICH_URL,
            json=payload,
            timeout=900,
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


def call_quarantine_process(**context) -> int:
    """Воркер карантина: лоты с низкой уверенностью Jev → DeepSeek → detail.ads."""
    url = AI_ENRICH_URL.replace("/batch", "/quarantine_process")
    payload = {"limit": QUARANTINE_LIMIT}
    try:
        resp = requests.post(url, json=payload, timeout=900)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise AirflowException(f"quarantine_process call failed: {e}")
    result = resp.json()
    done = result.get("done", 0)
    context["ti"].log.info(
        "quarantine: done=%s, failed=%s, new_tags=%s",
        done, result.get("failed", 0), result.get("new_tags", 0),
    )
    return done


default_args = {
    "owner": "data-eng",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "on_failure_callback": dag_failure_callback,
}

with DAG(
    dag_id="midraw_to_details",
    description="Call ai_enrich /batch to enrich all unprocessed midraw.avito_ads into detail.ads.",
    default_args=default_args,
    schedule="*/15 * * * *",
    start_date=datetime(2026, 8, 1),
    catchup=False,
    max_active_runs=1,
    tags=["avito", "ai", "midraw", "details"],
) as dag:
    enrich_batch = PythonOperator(
        task_id="ai_enrich_batch",
        python_callable=call_ai_enrich_batch,
    )

    # Карантин: лоты с низкой уверенностью Jev разбирает DeepSeek,
    # создаёт новые теги в словарь. После основной пачки.
    quarantine_process = PythonOperator(
        task_id="quarantine_process",
        python_callable=call_quarantine_process,
    )

    # Пересчёт состояния объявлений (активно/закрыто): активные = видели
    # в выдаче (midraw) за последние 48 часов. Запускается после обогащения.
    update_state = PythonOperator(
        task_id="update_ad_state",
        python_callable=_update_ad_state,
    )

    enrich_batch >> quarantine_process >> update_state
