#!/usr/bin/env bash
# ============================================================
# Тестовое восстановление последнего дампа в отдельную БД.
# Проверяет, что бэкап реально восстанавливается и данные на месте.
# БД avito_restore_test создаётся и удаляется — прод не трогается.
# ============================================================
set -uo pipefail

ROOT="/home/yury/backups/avito"
PG_CONT="airflow-avito-project-external-postgres-1"
TEST_DB="avito_restore_test"
MIN_LOTS=10000
LOG="$ROOT/backup.log"

log() { echo "$(date '+%F %T') [restore-test] $*" | tee -a "$LOG"; }

DUMP=$(ls -1t "$ROOT"/daily/*.dump 2>/dev/null | head -1)
[ -n "$DUMP" ] || { log "ОШИБКА: нет дампов"; exit 1; }
log "проверяю $DUMP"

psql() { docker exec "$PG_CONT" psql -U avito -d "$1" -t -A -c "$2"; }

psql postgres "DROP DATABASE IF EXISTS $TEST_DB" >/dev/null 2>&1
psql postgres "CREATE DATABASE $TEST_DB" >/dev/null || { log "ОШИБКА: create db"; exit 1; }

if ! docker exec -i "$PG_CONT" pg_restore -U avito -d "$TEST_DB" --no-owner < "$DUMP" 2>>"$LOG"; then
    log "ВНИМАНИЕ: pg_restore вернул ошибки (см. лог)"
fi

LOTS=$(psql "$TEST_DB" "SELECT count(*) FROM detail.ads" 2>/dev/null | tr -d ' ')
CONDS=$(psql "$TEST_DB" "SELECT count(*) FROM detail.ads WHERE condition IS NOT NULL" 2>/dev/null | tr -d ' ')
TAGS=$(psql "$TEST_DB" "SELECT count(*) FROM avito_original.tag_vocabulary WHERE status='active'" 2>/dev/null | tr -d ' ')

psql postgres "DROP DATABASE IF EXISTS $TEST_DB" >/dev/null 2>&1

if [ -n "$LOTS" ] && [ "$LOTS" -ge "$MIN_LOTS" ]; then
    log "ОК: восстановлено лотов=$LOTS, с condition=$CONDS, активных тегов=$TAGS; тестовая БД удалена"
    exit 0
else
    log "ПРОВАЛ: лотов=$LOTS (ожидалось >= $MIN_LOTS)"
    exit 1
fi
