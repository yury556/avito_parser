# -*- coding: utf-8 -*-
"""Бэкфилл detail.ads: старые свободные теги DeepSeek (JSON) →
tags_universal (слаги словаря) + tier='deepseek'. Идемпотентен:
трогает только строки, где tags_universal IS NULL.
Запуск: docker exec ai-stack-ai_enrich-1 python /app/backfill_tags_universal.py
"""

import json
import os
import re
import sys

import psycopg2

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "external-postgres"),
    port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ.get("DB_NAME", "avito_dwh"),
    user=os.environ.get("DB_USER", "avito"),
    password=os.environ.get("DB_PASSWORD", ""),
)
cur = conn.cursor()


def slug_tag(ru_text):
    s = (ru_text or "").strip().lower()
    s = re.sub(r"[^\wа-яё0-9]+", "-", s, flags=re.IGNORECASE)
    s = s.strip("-")[:40]
    return s


cur.execute("""
    SELECT avito_id, tags FROM detail.ads
    WHERE tags_universal IS NULL AND tags IS NOT NULL AND tags != '[]'::jsonb
""")
rows = cur.fetchall()
print(f"лотов к бэкфиллу: {len(rows)}")

vocab = {}
cur.execute("SELECT tag_key FROM avito_original.tag_vocabulary")
for (k,) in cur.fetchall():
    vocab[k] = True

updated = 0
new_tags = 0
for avito_id, tags_json in rows:
    try:
        tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
    except Exception:
        continue
    if not isinstance(tags, list):
        continue
    uni = []
    for t in tags:
        k = slug_tag(t)
        if k:
            uni.append(k)
            if k not in vocab:
                cur.execute("""
                    INSERT INTO avito_original.tag_vocabulary
                        (tag_key, grp, label_ru, created_by, status)
                    VALUES (%s, 'сигнал', %s, 'backfill', 'active')
                    ON CONFLICT (tag_key) DO NOTHING
                """, (k, t))
                if cur.rowcount:
                    new_tags += 1
                vocab[k] = True
    cur.execute("""
        UPDATE detail.ads
        SET tags_universal = %s, tier = COALESCE(tier, 'deepseek')
        WHERE avito_id = %s
    """, (uni, avito_id))
    updated += 1
    if updated % 2000 == 0:
        conn.commit()
        print(f"  ...{updated}")

conn.commit()
cur.execute("SELECT count(*) FROM detail.ads WHERE tags_universal IS NOT NULL AND array_length(tags_universal, 1) > 0")
print(f"готово: обновлено {updated}, новых тегов в словаре {new_tags}, "
      f"лотов с непустым tags_universal: {cur.fetchone()[0]}")
conn.close()
