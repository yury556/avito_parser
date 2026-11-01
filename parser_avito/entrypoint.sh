#!/bin/bash
set +e

if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "Starting uvicorn..."
    cd /app
    uvicorn avito_service:app --host 0.0.0.0 --port 8000 >> /app/storage/uvicorn.log 2>&1 &
    sleep 3
fi

cd /app
python3 -c "
import sys, time, os, json as json_lib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/app/src')
from avito_parser.config import load_avito_config, AvitoConfig
from avito_parser.core import AvitoParse
from parser_runs import finish_parser_run, new_run_id, start_parser_run

CONFIG_PATH = os.environ.get('PARSER_CONFIG', '/app/config/config.toml')

def _extract_image_urls(item):
    urls = []
    # 1. Try gallery (search results)
    try:
        gallery = getattr(item, 'gallery', None)
        if gallery:
            for url_attr in ('imageLargeVipUrl', 'imageLargeUrl', 'imageUrl', 'imageVipUrl'):
                url = getattr(gallery, url_attr, None)
                if url:
                    urls.append(str(url))
                    break
    except Exception:
        pass
    # 2. Try images list (detail pages)
    if not urls:
        try:
            images = getattr(item, 'images', None)
            if images:
                for img in images:
                    try:
                        root = getattr(img, 'root', None)
                        if not root:
                            continue
                        keys = list(root.keys())
                        if not keys:
                            continue
                        best_key = max(
                            keys,
                            key=lambda k: int(k.split("x")[0]) * int(k.split("x")[1])
                        )
                        urls.append(str(root[best_key]))
                    except Exception:
                        continue
        except Exception:
            pass
    return urls


def item_to_dict(item) -> dict:
    price_raw = item.priceDetailed.value if item.priceDetailed else None
    description_raw = item.description or ''
    if not description_raw and item.iva:
        steps = item.iva.get('DescriptionStep', [])
        if steps and steps[0].payload:
            description_raw = steps[0].payload.get('description', '') or ''
    description_clean = ' '.join(description_raw.replace('\n', ' ').replace('\r', ' ').split())[:2000] or None
    return {
        'avito_id': item.id if isinstance(item.id, int) else None,
        'title': item.title or '',
        'price_rub': int(price_raw) if price_raw is not None else None,
        'url': f'https://www.avito.ru{item.urlPath}' if item.urlPath else None,
        'location': (item.location.name or '')[:256] if item.location else None,
        'seller': (item.sellerId or '')[:64] if item.sellerId else None,
        'description': description_clean,
        'is_reserved': item.isReserved if item.isReserved is not None else False,
        'is_promotion': item.isPromotion if item.isPromotion is not None else False,
        'total_views': item.total_views,
        'today_views': item.today_views,
        'image_urls': _extract_image_urls(item),
    }

def postgres_config():
    return {
        'host': os.environ.get('POSTGRES_HOST', 'external-postgres'),
        'port': int(os.environ.get('POSTGRES_PORT', 5432)),
        'dbname': os.environ.get('POSTGRES_DB', 'avito_dwh'),
        'user': os.environ.get('POSTGRES_USER', 'avito'),
        'password': os.environ.get('POSTGRES_PASSWORD', 'avito123'),
    }


def write_to_postgres(ads: list[dict], observed_at=None):
    import psycopg2
    from psycopg2.extras import execute_values
    observed_at = observed_at or datetime.now(timezone.utc)
    conn = None
    try:
        conn = psycopg2.connect(**postgres_config())
        with conn.cursor() as cur:
            cur.execute('''
                ALTER TABLE avito_original.ads
                ADD COLUMN IF NOT EXISTS description TEXT
            ''')
            cur.execute('''
                ALTER TABLE avito_original.ads
                ADD COLUMN IF NOT EXISTS image_keys JSONB DEFAULT '[]'::jsonb
            ''')
            execute_values(
                cur,
                '''
                INSERT INTO avito_original.ads
                    (avito_id, title, price_rub, url, location, seller,
                     description, image_keys,
                     is_reserved, is_promotion, total_views, today_views,
                     parsed_at, first_seen_at, last_seen_at, content_updated_at)
                VALUES %s
                ON CONFLICT (avito_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    price_rub = EXCLUDED.price_rub,
                    url = EXCLUDED.url,
                    location = EXCLUDED.location,
                    seller = EXCLUDED.seller,
                    description = EXCLUDED.description,
                    is_reserved = EXCLUDED.is_reserved,
                    is_promotion = EXCLUDED.is_promotion,
                    total_views = EXCLUDED.total_views,
                    today_views = EXCLUDED.today_views,
                    parsed_at = CASE
                        WHEN avito_original.ads.price_rub IS DISTINCT FROM EXCLUDED.price_rub
                          OR avito_original.ads.is_reserved IS DISTINCT FROM EXCLUDED.is_reserved
                          OR avito_original.ads.is_promotion IS DISTINCT FROM EXCLUDED.is_promotion
                          OR avito_original.ads.total_views IS DISTINCT FROM EXCLUDED.total_views
                          OR avito_original.ads.today_views IS DISTINCT FROM EXCLUDED.today_views
                          OR avito_original.ads.description IS DISTINCT FROM EXCLUDED.description
                        THEN EXCLUDED.parsed_at
                        ELSE avito_original.ads.parsed_at
                    END,
                    first_seen_at = COALESCE(
                        avito_original.ads.first_seen_at, EXCLUDED.first_seen_at),
                    last_seen_at = EXCLUDED.last_seen_at,
                    content_updated_at = CASE
                        WHEN avito_original.ads.price_rub IS DISTINCT FROM EXCLUDED.price_rub
                          OR avito_original.ads.title IS DISTINCT FROM EXCLUDED.title
                          OR avito_original.ads.url IS DISTINCT FROM EXCLUDED.url
                          OR avito_original.ads.location IS DISTINCT FROM EXCLUDED.location
                          OR avito_original.ads.seller IS DISTINCT FROM EXCLUDED.seller
                          OR avito_original.ads.is_reserved IS DISTINCT FROM EXCLUDED.is_reserved
                          OR avito_original.ads.is_promotion IS DISTINCT FROM EXCLUDED.is_promotion
                          OR avito_original.ads.total_views IS DISTINCT FROM EXCLUDED.total_views
                          OR avito_original.ads.today_views IS DISTINCT FROM EXCLUDED.today_views
                          OR avito_original.ads.description IS DISTINCT FROM EXCLUDED.description
                        THEN EXCLUDED.content_updated_at
                        ELSE avito_original.ads.content_updated_at
                    END,
                    last_price_changed_at = CASE
                        WHEN avito_original.ads.price_rub IS DISTINCT FROM EXCLUDED.price_rub
                        THEN EXCLUDED.last_seen_at
                        ELSE avito_original.ads.last_price_changed_at
                    END
                ''',
                [
                    (
                        ad['avito_id'],
                        ad.get('title', ''),
                        ad.get('price_rub'),
                        ad.get('url', ''),
                        ad.get('location'),
                        ad.get('seller'),
                        ad.get('description'),
                        json_lib.dumps(ad.get('image_keys', [])),
                        ad.get('is_reserved', False),
                        ad.get('is_promotion', False),
                        ad.get('total_views'),
                        ad.get('today_views'),
                        observed_at,
                        observed_at,
                        observed_at,
                        observed_at,
                    )
                    for ad in ads
                    if ad.get('avito_id')
                ],
            )
            image_jobs = [
                (ad['avito_id'], url, position, 'image')
                for ad in ads if ad.get('avito_id')
                for position, url in enumerate(
                    ad.get('image_urls', [])[:int(os.environ.get('MAX_IMAGES_PER_AD', '10'))]
                ) if url
            ]
            if image_jobs:
                execute_values(
                    cur,
                    '''
                    INSERT INTO avito_original.image_jobs
                        (avito_id, original_url, position, job_type)
                    VALUES %s
                    ON CONFLICT (avito_id, original_url) DO NOTHING
                    ''',
                    image_jobs,
                )
        conn.commit()
        conn.close()
        valid = [ad for ad in ads if ad.get('avito_id')]
        print(f'Write to Postgres: {len(valid)} rows', flush=True)
        return len(valid)
    except Exception as e:
        if conn is not None:
            conn.rollback()
            conn.close()
        print(f'Postgres error: {e}', flush=True)
        raise

while True:
    run_id = None
    items = []
    written = 0
    partial = False
    run_error = None
    try:
        config = load_avito_config(CONFIG_PATH)
        config.save_json = True
        config.one_time_start = False
        config.count = config.count or 1
        parser = AvitoParse(config)
        print(f'=== Parse cycle === {len(config.urls)} URL (config: {CONFIG_PATH}) ===', flush=True)
        pg_config = postgres_config()
        run_id = new_run_id('cycle')
        start_parser_run(
            pg_config, run_id, len(config.urls),
            os.environ.get('PARSER_VERSION', '3.2.17'),
        )
        try:
            items = parser.parse()
        except Exception as parse_err:
            # Частичный урожай: лоты уже собранных страниц не теряем.
            # Postgres-запись идемпотентна (upsert по avito_id).
            items = getattr(parser, 'collected', None) or []
            partial = True
            run_error = parse_err
            if not items:
                raise
            print(f'Parse error: {parse_err}; сохраняю частичный урожай: {len(items)} ads', flush=True)
        if items:
            ads = [item_to_dict(item) for item in items]
            print(f'Got {len(ads)} ads', flush=True)
            # Изображения скачивает отдельный image-downloader через image_jobs.
            written = write_to_postgres(ads)
        # Пустой, но завершённый цикл — success, а не сбой.
        finish_parser_run(
            pg_config, run_id, 'partial' if partial else 'success',
            len(items), written, run_error,
        )
    except Exception as e:
        if run_id is not None:
            try:
                finish_parser_run(
                    postgres_config(), run_id, 'failed', len(items), written, e)
            except Exception as run_exc:
                print(f'Parser run finish error: {run_exc}', flush=True)
        print(f'Error: {e}', flush=True)
        time.sleep(30)
        continue
    print('=== Pause ===', flush=True)
    time.sleep(config.pause_general)
"
