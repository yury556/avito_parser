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
    return {
        'avito_id': item.id if isinstance(item.id, int) else None,
        'title': item.title or '',
        'price_rub': item.priceDetailed.value if item.priceDetailed else None,
        'url': f'https://www.avito.ru{item.urlPath}' if item.urlPath else None,
        'location': item.location.name if item.location else None,
        'seller': item.sellerId if item.sellerId else None,
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
            execute_values(
                cur,
                '''
                INSERT INTO avito_original.ads
                    (avito_id, title, price_rub, url, location, seller,
                     is_reserved, is_promotion, total_views, today_views, parsed_at)
                VALUES %s
                ON CONFLICT (avito_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    price_rub = EXCLUDED.price_rub,
                    url = EXCLUDED.url,
                    location = EXCLUDED.location,
                    seller = EXCLUDED.seller,
                    is_reserved = EXCLUDED.is_reserved,
                    is_promotion = EXCLUDED.is_promotion,
                    total_views = EXCLUDED.total_views,
                    today_views = EXCLUDED.today_views,
                    parsed_at = EXCLUDED.parsed_at
                ''',
                [
                    (
                        ad['avito_id'],
                        ad.get('title', ''),
                        ad.get('price_rub'),
                        ad.get('url', ''),
                        ad.get('location'),
                        ad.get('seller'),
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
