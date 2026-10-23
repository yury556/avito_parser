# -*- coding: utf-8 -*-
"""Чистка словаря тегов: слияние синонимов в канон, архив спеков.
Канон = 32 seed-тега + promoted (полезные не-спеки).
Всё остальное → status='archived' и удаляется из tags_universal лотов
(оригиналы остаются в tags JSON для аудита). Идемпотентен.
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

# --- слияние синонимов: тег-слаг в лотах → каноничный tag_key ---
MAP = {
    # русские варианты seed-ключей
    "новый": "novyy", "новая": "novyy", "новое": "novyy",
    "б-у": "bu", "бу": "bu",
    "как-новый": "kak-novyy",
    "запечатан": "zapechatan",
    "не-работает": "ne-rabotaet",
    "оригинал": "original",
    "глобальная-версия": "globalnaya-versiya",
    "полный-комплект": "polnyy-komplekt",
    "комплект": "polnyy-komplekt",
    "оем": "oem",
    "доставка": "dostavka",
    "самовывоз": "samovyvoz",
    "торг": "torg",
    "обмен": "obmen",
    "проверка": "proverka",
    "скол": "skol",
    "трещина": "treschina",
    "вмятины": "vmyatiny",
    "потёртости": "poter-pokrytie", "потертости": "poter-pokrytie",
    # состояния
    "отличное-состояние": "kak-novyy",
    "идеальное-состояние": "kak-novyy",
    "хорошее-состояние": "bu",
    "рабочее-состояние": "bu",
    "использовался": "ispolzovalsya",
    "наработка": "narabotka-malaya",
    # упаковка
    "не-вскрывалась": "zapechatan",
    "заводская-упаковка": "zapechatan",
    "коробка": "v-korobke",
    # гарантия
    "официальная-гарантия": "garantiya-magazina",
    "гарантия": "garantiya-magazina",
    # прочие синонимы
    "есть-дефект": "est-defekt",
    "царапина": "tsarapina",
    "без-дефектов": "bez-defektov",
    "сим-эсим": "neskolko-shtuk",  # 2 сим → не универсальный, архив ниже
}

# --- полезные не-seed теги, остаются каноничными ---
PROMOTED = {
    "bez-komplekta": "комплект",
    "v-nalichii": "сигнал",
    "pod-zakaz": "сигнал",
    "iz-ssha": "происхождение",
    "s-dokumentami": "сигнал",
    "rostest": "происхождение",
    "est-defekt": "дефект",
    "tsarapina": "дефект",
    "bez-defektov": "дефект",
    "bez-torga": "условия",
    "samosval": "сигнал",
    "tent": "сигнал",
    "modulnyy": "сигнал",
}

# 1) promoted → активные с правильной группой
for key, grp in PROMOTED.items():
    cur.execute("""
        INSERT INTO avito_original.tag_vocabulary (tag_key, grp, label_ru, created_by, status)
        VALUES (%s, %s, %s, 'cleanup', 'active')
        ON CONFLICT (tag_key) DO UPDATE SET grp = EXCLUDED.grp, status = 'active'
    """, (key, grp, key.replace("-", " ")))

# 2) канон = seed + promoted
cur.execute("SELECT tag_key FROM avito_original.tag_vocabulary WHERE created_by = 'seed'")
canonical = {r[0] for r in cur.fetchall()} | set(PROMOTED.keys())

# 3) пересборка массивов лотов
cur.execute("""
    SELECT avito_id, tags_universal FROM detail.ads
    WHERE tags_universal IS NOT NULL AND array_length(tags_universal, 1) > 0
""")
rows = cur.fetchall()
print(f"лотов с тегами: {len(rows)}")
recount = {}
kept_lots = 0
for i, (avito_id, tags) in enumerate(rows):
    new = []
    for t in tags:
        c = MAP.get(t, t)
        if c in canonical and c not in new:
            new.append(c)
            recount[c] = recount.get(c, 0) + 1
    cur.execute("UPDATE detail.ads SET tags_universal = %s WHERE avito_id = %s",
                (new, avito_id))
    if new:
        kept_lots += 1
    if i % 3000 == 0 and i > 0:
        conn.commit()
conn.commit()

# 4) архив всего неканонного, usage_count по факту
cur.execute("UPDATE avito_original.tag_vocabulary SET status = 'archived' WHERE created_by != 'seed' AND tag_key NOT IN %s",
            (tuple(canonical),))
for key, cnt in recount.items():
    cur.execute("UPDATE avito_original.tag_vocabulary SET usage_count = %s WHERE tag_key = %s",
                (cnt, key))
cur.execute("UPDATE avito_original.tag_vocabulary SET usage_count = 0 WHERE status = 'archived'")
conn.commit()

cur.execute("SELECT status, count(*) FROM avito_original.tag_vocabulary GROUP BY status")
print("словарь:", cur.fetchall())
cur.execute("""
    SELECT tag_key, usage_count FROM avito_original.tag_vocabulary
    WHERE status = 'active' ORDER BY usage_count DESC LIMIT 25
""")
print("топ канонных:")
for k, c in cur.fetchall():
    print(f"  {k:25} {c}")
cur.execute("SELECT count(*) FROM detail.ads WHERE array_length(tags_universal, 1) > 0")
print(f"лотов с канонными тегами: {cur.fetchone()[0]}")
conn.close()
