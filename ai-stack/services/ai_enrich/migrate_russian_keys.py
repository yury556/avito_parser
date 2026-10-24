# -*- coding: utf-8 -*-
"""Миграция словаря: tag_key с транслита (novyy) на русские метки (новый).
Ключ тега = label_ru. Теги лота (tags_universal) переписываются на новые
ключи. Архивные теги не трогаются (невидимы). Идемпотентен.
"""

import os
import psycopg2

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "external-postgres"),
    port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ.get("DB_NAME", "avito_dwh"),
    user=os.environ.get("DB_USER", "avito"),
    password=os.environ.get("DB_PASSWORD", ""),
)
cur = conn.cursor()

cur.execute("SELECT tag_key, label_ru FROM avito_original.tag_vocabulary WHERE status = 'active'")
rows = cur.fetchall()
print(f"активных тегов: {len(rows)}")

# старый ключ → новый ключ (label_ru); коллизии сливаем на первый
mapping = {}
merged = set()
for old_key, label in rows:
    if old_key == label:
        mapping[old_key] = old_key
        continue
    # если label уже занят другим активным ключом — старый уйдёт в архив
    taken = next((nk for nk, nl in mapping.items() if nl == label), None)
    if taken is None:
        mapping[old_key] = label
    else:
        mapping[old_key] = taken
        merged.add(old_key)

new_keys = {v for v in mapping.values()}

# 0) освобождаем новые ключи: архивные/чужие строки с такими ключами → arch_префикс
cur.execute("SELECT tag_key FROM avito_original.tag_vocabulary WHERE status != 'active' AND tag_key = ANY(%s)",
            (list(new_keys),))
for clash in [r[0] for r in cur.fetchall()]:
    cur.execute("UPDATE avito_original.tag_vocabulary SET tag_key = 'arch_' || tag_key WHERE tag_key = %s",
                (clash,))

# 1) переписываем ключи в словаре (сначала снимаем уникальность конфликты)
for old_key, new_key in mapping.items():
    if old_key == new_key:
        continue
    if new_key in merged:
        continue
    cur.execute("""
        UPDATE avito_original.tag_vocabulary SET tag_key = %s WHERE tag_key = %s
    """, (new_key, old_key))

# 2) слитые дубликаты → архив
for dup in merged:
    cur.execute("UPDATE avito_original.tag_vocabulary SET status='archived' WHERE tag_key=%s", (dup,))

# 3) переписываем массивы лотов: старый ключ → новый, дедупликация
cur.execute("SELECT avito_id, tags_universal FROM detail.ads WHERE tags_universal IS NOT NULL")
lots = cur.fetchall()
print(f"лотов с тегами: {len(lots)}")
for i, (avito_id, tags) in enumerate(lots):
    new = []
    for t in tags:
        n = mapping.get(t, t)
        if n and n not in new:
            new.append(n)
    if new != tags:
        cur.execute("UPDATE detail.ads SET tags_universal = %s WHERE avito_id = %s",
                    (new, avito_id))
    if i % 3000 == 0 and i > 0:
        conn.commit()
conn.commit()

# 4) финальные проверки
cur.execute("SELECT count(*) FROM avito_original.tag_vocabulary WHERE status='active'")
print("активных после:", cur.fetchone()[0])
cur.execute("""
    SELECT tag_key, label_ru, tag_key = label_ru AS совпадает
    FROM avito_original.tag_vocabulary WHERE status='active' LIMIT 5
""")
for r in cur.fetchall():
    print(" ", r)
conn.close()
print("готово")
