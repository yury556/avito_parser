"""Бэкфилл детерминированной валидации модели по URL-slug.

Проходит по detail.ads, для каждого лота сверяет модель из LLM с чипом
из URL-slug и правит модель, если LLM назвал другой чип того же типа.
Оригинал сохраняется в detail.ads.model_ai, чип — в detail.ads.chip.

Запуск (внутри контейнера ai_enrich, есть доступ к БД):
    python backfill_model_guard.py [--dry-run]
"""
import os
import sys

import psycopg2

from model_guard import validate_model

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5433"))
DB_NAME = os.getenv("DB_NAME", "avito_dwh")
DB_USER = os.getenv("DB_USER", "avito")
DB_PASSWORD = os.getenv("DB_PASSWORD", "avito123")
DB = dict(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)

DRY = "--dry-run" in sys.argv


def main() -> None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT d.avito_id, d.model, m.url
            FROM detail.ads d
            JOIN midraw.avito_ads m ON m.avito_id = d.avito_id
            WHERE d.model IS NOT NULL AND d.model != ''
        """)
        rows = cur.fetchall()
        print(f"проверяю {len(rows)} лотов (dry_run={DRY})", flush=True)

        fixed = 0
        samples = []
        for avito_id, model, url in rows:
            new_model, model_ai, chip, note = validate_model(model, url)
            if not note:
                continue
            fixed += 1
            if len(samples) < 15:
                samples.append((avito_id, model, new_model, note))
            if not DRY:
                cur.execute(
                    "UPDATE detail.ads SET model=%s, model_ai=%s, chip=%s WHERE avito_id=%s",
                    (new_model, model_ai, chip, avito_id))
        if not DRY:
            conn.commit()

        print(f"исправлено: {fixed} из {len(rows)} ({fixed * 100 // max(len(rows),1)}%)", flush=True)
        for avito_id, old, new, note in samples:
            print(f"  {avito_id}: {old!r} -> {new!r}  ({note})", flush=True)


if __name__ == "__main__":
    main()
