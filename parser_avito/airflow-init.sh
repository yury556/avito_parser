#!/bin/bash
set -e

airflow db init

airflow users create \
    --username admin \
    --password admin \
    --firstname Admin \
    --lastname User \
    --role Admin \
    --email admin@example.com 2>/dev/null || echo "User already exists"

airflow connections add 'avito_postgres' \
    --conn-type 'postgres' \
    --conn-host 'postgres' \
    --conn-port '5432' \
    --conn-login 'avito' \
    --conn-password 'avito123' \
    --conn-schema 'avito' 2>/dev/null || echo "Connection already exists"