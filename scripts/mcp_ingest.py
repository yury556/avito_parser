#!/usr/bin/env python3
"""Ingester для mcp-avito: JSON циклов → avito_original.ads (+ фото из details).

Режимы:
  python3 mcp_ingest.py            # вглотать новые cycle-файлы
  python3 mcp_ingest.py --details  # вглотать details-файлы (описание+фото)

Дедуп: файлы отмечаются в state-файле; лоты — по avito_id в БД.
Запускается systemd-таймером каждые 10-15 минут.
"""
import glob
import io
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

import psycopg2
import requests

ROOT = "/home/yury/mcp-avito"
RESULTS = os.path.join(ROOT, "mcp_avito_results")
STATE = os.path.join(ROOT, ".ingested")
DETAILS_DIR = os.path.join(RESULTS, "details")
DETAILS_STATE = os.path.join(ROOT, ".details_ingested")
PENDING = os.path.join(ROOT, "pending_details.txt")

DB = dict(host="127.0.0.1", port=5433, dbname="avito_dwh",
          user="avito", password="avito123")
MINIO = dict(endpoint="127.0.0.1:9000", access="minioadmin", secret="minioadmin",
             bucket="avito-images")
# url в image_keys — в docker-сети (как у парсера)
MINIO_URL_PREFIX = "minio:9000/avito-images"


def load_state(path):
    try:
        return set(open(path).read().splitlines())
    except FileNotFoundError:
        return set()


def save_state(path, done):
    with open(path, "a") as f:
        f.write("\n".join(sorted(done)) + "\n")


def db_conn():
    return psycopg2.connect(**DB)


def is_spb(item):
    url = item.get("url") or ""
    loc = (item.get("location") or "").lower()
    return "/sankt-peterburg" in url or "петербург" in loc or "санкт" in loc


def search_url_for(query):
    q = urllib.parse.quote_plus(query)
    return f"https://www.avito.ru/sankt-peterburg?q={q}&s=104"


def ingest_cycles():
    state = load_state(STATE)
    files = sorted(f for f in glob.glob(os.path.join(RESULTS, "cycle-*", "*.json"))
                   if not f.endswith("cycle-summary.json") and f not in state)
    if not files:
        print("нет новых cycle-файлов")
        return

    rows = []          # (avito_id, dict) новые
    source_pairs = {}  # avito_id -> source_url
    done_files = set()
    for f in files:
        try:
            data = json.load(open(f))
        except Exception as e:
            print(f"битый файл {f}: {e}")
            done_files.add(f)
            continue
        src = search_url_for(data.get("query", ""))
        for item in data.get("results", []):
            if not is_spb(item):
                continue
            try:
                avito_id = int(item["id"])
            except (KeyError, ValueError):
                continue
            url = (item.get("url") or "").split("?")[0]
            rows.append((avito_id, dict(
                title=(item.get("title") or "")[:512],
                price_rub=item.get("price"),
                url=url,
                location=(item.get("location") or None),
                description=None,
            )))
            source_pairs[avito_id] = src
        done_files.add(f)

    if not rows:
        save_state(STATE, done_files)
        print(f"новых СПб-лотов 0 (файлов: {len(done_files)})")
        return

    # дедуп внутри пачки (лот может прийти из нескольких поисков)
    uniq = {}
    for aid, d in rows:
        uniq.setdefault(aid, d)
    rows = list(uniq.items())

    conn = db_conn()
    new_ids = []
    try:
        with conn.cursor() as cur:
            from psycopg2.extras import execute_values
            execute_values(cur, """
                INSERT INTO avito_original.ads
                    (avito_id, title, price_rub, url, location, seller,
                     description, image_keys, source_url,
                     is_reserved, is_promotion, total_views, today_views, parsed_at)
                VALUES %s
                ON CONFLICT (avito_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    price_rub = EXCLUDED.price_rub,
                    url = EXCLUDED.url,
                    location = EXCLUDED.location,
                    source_url = COALESCE(EXCLUDED.source_url, avito_original.ads.source_url),
                    parsed_at = EXCLUDED.parsed_at
                WHERE avito_original.ads.price_rub IS DISTINCT FROM EXCLUDED.price_rub
            """, [
                (aid, d["title"], d["price_rub"], d["url"], d["location"], None,
                 d["description"], json.dumps([]), source_pairs[aid],
                 False, False, None, None, datetime.now(timezone.utc))
                for aid, d in rows
            ])
            ids = [aid for aid, _ in rows]
            cur.execute("SELECT avito_id FROM avito_original.ads WHERE avito_id = ANY(%s)", [ids])
            known = {r[0] for r in cur.fetchall()}
            # «новые» = не существовали до вставки
            cur.execute("""
                SELECT avito_id FROM avito_original.lot_pipeline_log
                WHERE avito_id = ANY(%s) AND stage = 'raw'
            """, [ids])
            already_logged = {r[0] for r in cur.fetchall()}
            new_ids = [i for i in ids if i not in already_logged]
            if new_ids:
                values = ", ".join(
                    f"(now(), {i}, 'raw', '{json.dumps({'run_id': 'mcp'})}'::jsonb)"
                    for i in new_ids)
                cur.execute(
                    f"INSERT INTO avito_original.lot_pipeline_log (ts, avito_id, stage, meta) "
                    f"VALUES {values} ON CONFLICT DO NOTHING")
            pairs = [(aid, source_pairs[aid]) for aid in new_ids if source_pairs.get(aid)]
            if pairs:
                execute_values(cur, """
                    INSERT INTO avito_original.lot_source (avito_id, source_url)
                    VALUES %s ON CONFLICT (avito_id) DO UPDATE
                      SET source_url = EXCLUDED.source_url, last_seen = now()
                """, pairs)
        conn.commit()
    finally:
        conn.close()

    save_state(STATE, done_files)
    # новые без описания → в очередь на details
    with open(PENDING, "a") as pf:
        for aid, d in rows:
            if aid in new_ids:
                pf.write(f"{aid}\t{d['url']}\n")
    print(f"вставлено строк: {len(rows)}, новых лотов: {len(new_ids)}")


def ingest_details():
    try:
        from minio import Minio
    except ImportError:
        print("minio не установлен")
        return
    client = Minio(MINIO["endpoint"], access_key=MINIO["access"],
                   secret_key=MINIO["secret"], secure=False)
    state = load_state(DETAILS_STATE)
    files = sorted(f for f in glob.glob(os.path.join(DETAILS_DIR, "*.json"))
                   if f not in state)
    if not files:
        print("нет новых details-файлов")
        return
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"})
    conn = db_conn()
    done = set()
    updated = 0
    try:
        with conn.cursor() as cur:
            for f in files:
                try:
                    d = json.load(open(f))
                except Exception:
                    done.add(f)
                    continue
                aid = d.get("avito_id")
                if not aid:
                    done.add(f)
                    continue
                desc = (d.get("description") or "").replace("\n", " ").replace("\r", " ")
                desc = " ".join(desc.split())[:10000] or None
                keys = []
                for i, img in enumerate((d.get("images") or [])[:10]):
                    u = img.get("url") or img.get("original_url") or ""
                    if not u:
                        continue
                    if u.startswith("//"):
                        u = "https:" + u
                    try:
                        r = sess.get(u, timeout=20)
                        r.raise_for_status()
                        ct = r.headers.get("Content-Type", "image/jpeg")
                        ext = {"image/jpeg": "jpg", "image/png": "png",
                               "image/webp": "webp"}.get(ct, "jpg")
                        key = f"{aid}/{i}.{ext}"
                        client.put_object(MINIO["bucket"], key, io.BytesIO(r.content),
                                          length=len(r.content), content_type=ct)
                        keys.append({"key": key, "url": f"{MINIO_URL_PREFIX}/{key}",
                                     "original_url": u, "size_bytes": len(r.content),
                                     "content_type": ct})
                    except Exception:
                        continue
                cur.execute("""
                    UPDATE avito_original.ads
                    SET description = COALESCE(%s, description),
                        image_keys = CASE WHEN %s::jsonb = '[]'::jsonb
                                          THEN image_keys ELSE %s::jsonb END
                    WHERE avito_id = %s
                """, (desc, json.dumps(keys), json.dumps(keys), aid))
                updated += 1
                done.add(f)
        conn.commit()
    finally:
        conn.close()
    save_state(DETAILS_STATE, done)
    print(f"details применено: {updated}")


if __name__ == "__main__":
    os.makedirs(DETAILS_DIR, exist_ok=True)
    if "--details" in sys.argv:
        ingest_details()
    else:
        ingest_cycles()
