import os
import sys
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
        save_xlsx=body.get("save_xlsx", False),
        use_webdriver=body.get("use_webdriver", False),
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
        "parsed_at": None,
    }


@app.post("/parse")
def parse_ads(body: dict):
    logger.info(f"Получен запрос на парсинг: {len(body.get('urls', []))} URL, "
                f"max_pages={body.get('max_pages', 1)}")

    config = _build_config(body)
    parser = AvitoParse(config)
    items = parser.parse()

    ads = [_item_to_dict(item) for item in items]
    logger.info(f"Парсинг завершён: {len(ads)} объявлений")

    return {"status": "ok", "count": len(ads), "ads": ads}


@app.get("/health")
def health():
    return {"status": "ok", "service": "avito-parser", "version": "3.2.16"}