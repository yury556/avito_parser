from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from avito_pipeline.config import AvitoConfig
from avito_pipeline.csv_contract import AvitoAd

AVITO_PARSER_SERVICE_URL = "http://avito-parser-service:8000/parse"
PARSER_RESULT_PATH = Path("/opt/airflow/parser_result/ads.json")


def parse_avito_search(config: AvitoConfig) -> list[AvitoAd]:
    payload = {
        "urls": config.urls,
        "max_pages": config.max_pages,
        "request_timeout": config.request_timeout,
        "user_agent": config.user_agent,
        "proxy_string": config.proxy_string,
        "proxy_change_url": config.proxy_change_url,
        "cookies_api_key": config.cookies_api_key,
        "use_bypass_api": config.use_bypass_api,
        "use_own_cookies": config.use_own_cookies,
        "min_price": config.min_price,
        "max_price": config.max_price,
        "white_list": config.white_list,
        "black_list": config.black_list,
        "seller_black_list": config.seller_black_list,
        "max_age": config.max_age,
        "ignore_reserv": config.ignore_reserv,
        "ignore_promotion": config.ignore_promotion,
        "parse_views": config.parse_views,
        "retry_delay": config.retry_delay,
        "block_threshold": config.block_threshold,
        "pause_between_links": config.pause_between_links,
    }

    response = requests.post(AVITO_PARSER_SERVICE_URL, json=payload, timeout=300)
    response.raise_for_status()

    data = response.json()

    parsed_at = datetime.now(timezone.utc)
    ads = []
    for ad in data.get("ads", []):
        ads.append(
            AvitoAd(
                avito_id=ad.get("avito_id"),
                title=ad.get("title", ""),
                price_rub=ad.get("price_rub"),
                url=ad.get("url", ""),
                location=ad.get("location"),
                seller=ad.get("seller"),
                parsed_at=parsed_at,
            )
        )

    return ads


def load_ads_from_json_file(path: Path = PARSER_RESULT_PATH) -> list[AvitoAd]:
    if not path.exists():
        return []
    parsed_at = datetime.now(timezone.utc)
    data = json.loads(path.read_text(encoding="utf-8"))
    ads = [
        AvitoAd(
            avito_id=ad["avito_id"],
            title=ad.get("title", ""),
            price_rub=ad.get("price_rub"),
            url=ad.get("url", ""),
            location=ad.get("location"),
            seller=ad.get("seller"),
            parsed_at=parsed_at,
        )
        for ad in data
        if ad.get("avito_id")
    ]
    return ads