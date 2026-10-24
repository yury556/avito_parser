# -*- coding: utf-8 -*-
"""Бэкфилл chip из URL-slug для всех лотов detail.ads, где чип пуст.
Модель (model_fixed) НЕ трогается — только chip. Идемпотентен.
"""

import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_guard import validate_model  # noqa: E402

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "external-postgres"),
    port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ.get("DB_NAME", "avito_dwh"),
    user=os.environ.get("DB_USER", "avito"),
    password=os.environ.get("DB_PASSWORD", ""),
)
cur = conn.cursor()
cur.execute("""
    SELECT DISTINCT ON (d.avito_id)
           d.avito_id, coalesce(NULLIF(m.url, ''), '')
    FROM detail.ads d
    LEFT JOIN midraw.avito_ads m ON m.avito_id = d.avito_id AND coalesce(m.url,'') != ''
    WHERE coalesce(d.chip, '') = ''
    ORDER BY d.avito_id, m.parsed_at DESC
""")
rows = cur.fetchall()
print(f"лотов без чипа: {len(rows)}")

filled = 0
for i, (avito_id, url) in enumerate(rows):
    if not url:
        continue
    _mf, _mai, chip, _note = validate_model(None, url)
    if chip:
        cur.execute("UPDATE detail.ads SET chip = %s WHERE avito_id = %s", (chip, avito_id))
        filled += 1
    if i % 2000 == 0 and i > 0:
        conn.commit()
        print(f"  ...{i}")
conn.commit()

cur.execute("SELECT count(*) FROM detail.ads WHERE chip IS NOT NULL AND chip != ''")
print(f"готово: заполнено {filled}, всего с чипом: {cur.fetchone()[0]}")
conn.close()
