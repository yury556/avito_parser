#!/bin/bash
set +e

if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "Starting uvicorn..."
    cd /app
    uvicorn avito_service:app --host 0.0.0.0 --port 8000 2>/dev/null &
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

CONFIG_PATH = os.environ.get('PARSER_CONFIG', '/app/config.toml')

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
    }

def write_to_postgres(ads: list[dict]):
    import psycopg2
    from psycopg2.extras import execute_values
    pg_config = {
        'host': os.environ.get('POSTGRES_HOST', 'external-postgres'),
        'port': int(os.environ.get('POSTGRES_PORT', 5432)),
        'dbname': os.environ.get('POSTGRES_DB', 'avito_dwh'),
        'user': os.environ.get('POSTGRES_USER', 'avito'),
        'password': os.environ.get('POSTGRES_PASSWORD', 'avito123'),
    }
    try:
        conn = psycopg2.connect(**pg_config)
        with conn.cursor() as cur:
            cur.execute('''
                ALTER TABLE avito_original.ads
                ADD COLUMN IF NOT EXISTS description TEXT
            ''')
            execute_values(
                cur,
                '''
                INSERT INTO avito_original.ads
                    (avito_id, title, price_rub, url, location, seller,
                     description,
                     is_reserved, is_promotion, total_views, today_views, parsed_at)
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
                    parsed_at = EXCLUDED.parsed_at
                WHERE avito_original.ads.price_rub     IS DISTINCT FROM EXCLUDED.price_rub
                   OR avito_original.ads.is_reserved   IS DISTINCT FROM EXCLUDED.is_reserved
                   OR avito_original.ads.is_promotion  IS DISTINCT FROM EXCLUDED.is_promotion
                   OR avito_original.ads.total_views   IS DISTINCT FROM EXCLUDED.total_views
                   OR avito_original.ads.today_views   IS DISTINCT FROM EXCLUDED.today_views
                   OR avito_original.ads.description IS DISTINCT FROM EXCLUDED.description
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
                        ad.get('is_reserved', False),
                        ad.get('is_promotion', False),
                        ad.get('total_views'),
                        ad.get('today_views'),
                        datetime.now(timezone.utc),
                    )
                    for ad in ads
                    if ad.get('avito_id')
                ],
            )
        conn.commit()
        conn.close()
        valid = [ad for ad in ads if ad.get('avito_id')]
        print(f'Write to Postgres: {len(valid)} rows', flush=True)
    except Exception as e:
        print(f'Postgres error: {e}', flush=True)

while True:
    try:
        config = load_avito_config(CONFIG_PATH)
        config.save_json = True
        config.one_time_start = False
        config.count = config.count or 1
        parser = AvitoParse(config)
        print('=== Parse cycle ===', flush=True)
        items = parser.parse()
        if items:
            ads = [item_to_dict(item) for item in items]
            print(f'Got {len(ads)} ads', flush=True)
            write_to_postgres(ads)
        print('=== Pause ===', flush=True)
        time.sleep(config.pause_general)
    except Exception as e:
        print(f'Error: {e}', flush=True)
        time.sleep(30)
"
