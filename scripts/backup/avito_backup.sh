#!/usr/bin/env bash
# ============================================================
# Ночной бэкап avito_parser: PostgreSQL + конфиги + ротация.
# Схема: 14 дневных + 8 недельных + 6 месячных (фиксированные 28 файлов).
# Запуск: systemd timer avito-backup.timer (03:30).
# ============================================================
set -uo pipefail

ROOT="/home/yury/backups/avito"
REPO="/home/yury/avito_parser"
PG_CONT="airflow-avito-project-external-postgres-1"
DB="avito_dwh"
PGUSER="avito"
TS="$(date +%F)"
DOW="$(date +%u)"          # 7 = воскресенье
DOM="$(date +%d)"          # последний день месяца?
MONTH="$(date +%Y-%m)"

DAILY_KEEP=14
WEEKLY_KEEP=8
MONTHLY_KEEP=6
MIN_SIZE=$((5 * 1024 * 1024))    # 5 МБ — сжатый дамп базы (сырая ~90 МБ)

LOG="$ROOT/backup.log"
mkdir -p "$ROOT"/{daily,weekly,monthly}
chown -R yury:yury "$ROOT"
chmod 700 "$ROOT"
touch "$LOG"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
fail() { log "ОШИБКА: $*"; exit 1; }

# защита от параллельного запуска
exec 9>"$ROOT/.lock"
flock -n 9 || { log "уже выполняется, выходим"; exit 0; }

log "=== старт бэкапа $TS ==="

# ---------- 1. PostgreSQL ----------
DUMP="$ROOT/daily/$TS.dump"
TMP="$DUMP.tmp"
if ! docker exec "$PG_CONT" pg_dump -U "$PGUSER" -d "$DB" -Fc > "$TMP" 2>>"$LOG"; then
    rm -f "$TMP"; fail "pg_dump"
fi
SIZE=$(stat -c%s "$TMP")
if [ "$SIZE" -lt "$MIN_SIZE" ]; then
    rm -f "$TMP"; fail "дамп слишком мал: $SIZE байт (< 50 МБ)"
fi
# проверка целостности: читаем оглавление архива
if ! docker exec -i "$PG_CONT" pg_restore -l < "$TMP" >/dev/null 2>>"$LOG"; then
    rm -f "$TMP"; fail "pg_restore -l не читает дамп (битый архив)"
fi
mv "$TMP" "$DUMP"
chmod 600 "$DUMP"
log "PostgreSQL: $DUMP ($(numfmt --to=iec "$SIZE")) — целостность ок"

# ---------- 2. Конфиги и секреты ----------
CFG="$ROOT/daily/$TS.config.tar.gz"
TMPCFG="$CFG.tmp"
tar -czf "$TMPCFG" -C "$REPO" \
    parser_avito/config/config.toml \
    ai-stack/.env \
    airflow-avito-project/docker-compose.yml 2>>"$LOG" \
    || { rm -f "$TMPCFG"; fail "архив конфигов"; }
mv "$TMPCFG" "$CFG"; chmod 600 "$CFG"
log "конфиги: $CFG"

# ---------- 3. Ротация ----------
# недельная копия — по воскресеньям
if [ "$DOW" = "7" ]; then
    cp -p "$DUMP" "$ROOT/weekly/$(date +%G-W%V).dump"
    chmod 600 "$ROOT/weekly/"*.dump
    log "недельная копия: $(date +%G-W%V)"
fi
# месячная — в последний день месяца
TOMORROW_DOM=$(date -d tomorrow +%d)
if [ "$TOMORROW_DOM" = "01" ]; then
    cp -p "$DUMP" "$ROOT/monthly/$MONTH.dump"
    chmod 600 "$ROOT/monthly/"*.dump
    log "месячная копия: $MONTH"
fi

prune() {
    local dir="$1" keep="$2"
    local count
    count=$(ls -1 "$dir"/*.dump 2>/dev/null | wc -l)
    if [ "$count" -gt "$keep" ]; then
        ls -1t "$dir"/*.dump | tail -n +$((keep + 1)) | while read -r old; do
            rm -f "$old"
            log "удалён старый: $(basename "$old")"
        done
    fi
}
prune "$ROOT/daily" "$DAILY_KEEP"
prune "$ROOT/weekly" "$WEEKLY_KEEP"
prune "$ROOT/monthly" "$MONTHLY_KEEP"

# конфиг-архивы: держим столько же, сколько дневных
count=$(ls -1 "$ROOT/daily/"*.config.tar.gz 2>/dev/null | wc -l)
if [ "$count" -gt "$DAILY_KEEP" ]; then
    ls -1t "$ROOT/daily/"*.config.tar.gz | tail -n +$((DAILY_KEEP + 1)) | while read -r old; do
        rm -f "$old"; log "удалён старый конфиг: $(basename "$old")"
    done
fi

chown -R yury:yury "$ROOT"
TOTAL=$(du -sh "$ROOT" | cut -f1)
FILES=$(find "$ROOT" -maxdepth 2 -name '*.dump' | wc -l)
log "=== готово: файлов дампов $FILES, всего $TOTAL ==="
