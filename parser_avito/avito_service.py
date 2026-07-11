import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastapi import FastAPI
from loguru import logger

from avito_parser.settings import AvitoConfig
from avito_parser.core import AvitoParse

app = FastAPI(title="Avito Parser Service", version="3.2.16")


def _build_config(body: dict) -> AvitoConfig:
    return AvitoConfig(
        urls=body.get("urls", []),
        count=body.get("max_pages", body.get("count", 1)),
        timeout=body.get("request_timeout", body.get("timeout", 20)),
        proxy_string=body.get("proxy_string"),
        proxy_change_url=body.get("proxy_change_url"),
        keys_word_white_list=body.get("keys_word_white_list", body.get("white_list", [])),
        keys_word_black_list=body.get("keys_word_black_list", body.get("black_list", [])),
        seller_black_list=body.get("seller_black_list", []),
        max_price=body.get("max_price", 999_999_999),
        min_price=body.get("min_price", 0),
        geo=body.get("geo"),
        max_age=body.get("max_age", 24 * 60 * 60),
        pause_between_links=body.get("pause_between_links", 5),
        max_count_of_retry=body.get("max_count_of_retry", 5),
        ignore_reserv=body.get("ignore_reserv", True),
        ignore_promotion=body.get("ignore_promotion", False),
        one_time_start=body.get("one_time_start", True),
        one_file_for_link=body.get("one_file_for_link", False),
        parse_views=body.get("parse_views", False),
        save_xlsx=False,
        save_json=body.get("save_json", True),
        use_webdriver=False,
        use_bypass_api=body.get("use_bypass_api", False),
        cookies_api_key=body.get("cookies_api_key"),
        output_dir=Path(body.get("output_dir", "result")),
        use_own_cookies=body.get("use_own_cookies", False),
        parse_phone=body.get("parse_phone", False),
        retry_delay=body.get("retry_delay", 5),
        block_threshold=body.get("block_threshold", 3),
        debug_mode=body.get("debug_mode", 0),
        user_agent=body.get("user_agent",
                           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/140.0.0.0 Safari/537.36"),
    )


def _item_to_dict(item) -> dict:
    return {
        "avito_id": item.id if isinstance(item.id, int) else None,
        "title": item.title or "",
        "price_rub": item.priceDetailed.value if item.priceDetailed else None,
        "url": f"https://www.avito.ru{item.urlPath}" if item.urlPath else None,
        "location": item.location.name if item.location else None,
        "seller": item.sellerId if item.sellerId else None,
        "is_reserved": item.isReserved if item.isReserved is not None else False,
        "is_promotion": item.isPromotion if item.isPromotion is not None else False,
        "total_views": item.total_views,
        "today_views": item.today_views,
    }


def _get_postgres_config(body: dict) -> dict | None:
    pg = body.get("postgres", {})
    if not pg.get("host") and not os.environ.get("POSTGRES_HOST"):
        return None
    return {
        "host": pg.get("host", os.environ.get("POSTGRES_HOST", "external-postgres")),
        "port": pg.get("port", int(os.environ.get("POSTGRES_PORT", 5432))),
        "dbname": pg.get("dbname", os.environ.get("POSTGRES_DB", "avito_dwh")),
        "user": pg.get("user", os.environ.get("POSTGRES_USER", "avito")),
        "password": pg.get("password", os.environ.get("POSTGRES_PASSWORD", "avito123")),
    }


@contextmanager
def _postgres_conn(pg_config: dict):
    import psycopg2
    conn = psycopg2.connect(**pg_config)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_ads_to_postgres(pg_config: dict, run_id: str, source_urls: list[str], ads: list[dict]) -> int:
    with _postgres_conn(pg_config) as conn:
        with conn.cursor() as cur:
            if ads:
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    """
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
                    """,
                    [
                        (
                            ad["avito_id"],
                            ad.get("title", ""),
                            ad.get("price_rub"),
                            ad.get("url", ""),
                            ad.get("location"),
                            ad.get("seller"),
                            ad.get("is_reserved", False),
                            ad.get("is_promotion", False),
                            ad.get("total_views"),
                            ad.get("today_views"),
                            datetime.now(timezone.utc),
                        )
                        for ad in ads
                        if ad.get("avito_id")
                    ],
                )
    return len([ad for ad in ads if ad.get("avito_id")])


@app.post("/parse")
def parse_ads(body: dict):
    logger.info(f"Получен запрос на парсинг: {len(body.get('urls', []))} URL, "
                f"max_pages={body.get('max_pages', 1)}")

    config = _build_config(body)
    parser = AvitoParse(config)
    items = parser.parse()

    ads = [_item_to_dict(item) for item in items]
    logger.info(f"Парсинг завершён: {len(ads)} объявлений")

    pg_config = _get_postgres_config(body)
    if pg_config and ads:
        run_id = datetime.now(timezone.utc).strftime("svc_%Y%m%d_%H%M%S")
        count = _write_ads_to_postgres(pg_config, run_id, config.urls, ads)
        logger.info(f"Записано в Postgres: {count} строк, run_id={run_id}")

    return {"status": "ok", "count": len(ads), "ads": ads}


@app.get("/health")
def health():
    return {"status": "ok", "service": "avito-parser", "version": "3.2.16"}