"""
Тестовый DAG: проверяет доступность PostgreSQL и пишет отчёт в raw._dag_log.

Запускается раз в час, но можно запустить вручную из UI Airflow.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

# Настройки по умолчанию
default_args = {
    "owner": "avito",
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
}

def check_and_log():
    """Подключается к PostgreSQL, считает записи, пишет лог."""
    hook = PostgresHook(postgres_conn_id="avito_postgres")

    # Создаём схему raw, если нет
    hook.run("CREATE SCHEMA IF NOT EXISTS raw;")

    # Создаём таблицу логов, если нет
    hook.run("""
        CREATE TABLE IF NOT EXISTS raw._dag_log (
            run_id          TEXT,
            ads_count       INTEGER,
            price_changes   INTEGER,
            executed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Считаем объявления
    ads_count = hook.get_first("SELECT COUNT(*) FROM ads")[0]

    # Считаем записи в price_history
    price_changes = hook.get_first("SELECT COUNT(*) FROM price_history")[0]

    # Получаем run_id
    run_id = "{{ run_id }}"

    # Пишем лог
    hook.run(
        "INSERT INTO raw._dag_log (run_id, ads_count, price_changes) VALUES (%s, %s, %s)",
        parameters=(run_id, ads_count, price_changes),
    )

    print(f"✅ ads: {ads_count}, price_history: {price_changes}")


with DAG(
    dag_id="avito_test_connection",
    default_args=default_args,
    description="Проверка: доходят ли данные из парсера до DWH-слоя",
    schedule="0 * * * *",         # каждый час
    start_date=datetime(2026, 6, 22),
    catchup=False,
    tags=["avito", "test"],
) as dag:

    test_task = PythonOperator(
        task_id="check_and_log_to_raw",
        python_callable=check_and_log,
    )
