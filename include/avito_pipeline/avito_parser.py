from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from avito_pipeline.config import AvitoConfig
from avito_pipeline.csv_contract import AvitoAd


AVITO_HOST = "https://www.avito.ru"


class AvitoParserError(RuntimeError):
    pass


def _digits_to_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"\D+", "", text)
    return int(digits) if digits else None


def _ad_id_from_url(url: str) -> int | None:
    match = re.search(r"_(\d+)(?:\?|$)", url)
    return int(match.group(1)) if match else None


def parse_avito_html(html: str, source_url: str) -> list[AvitoAd]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-marker="item"]')
    ads: list[AvitoAd] = []
    parsed_at = datetime.now(timezone.utc)

    for card in cards:
        link = card.select_one('[data-marker="item-title"], a[itemprop="url"], a[href*="/"]')
        title = link.get_text(" ", strip=True) if link else ""
        href = link.get("href") if link else ""
        full_url = urljoin(AVITO_HOST, href)
        avito_id = _ad_id_from_url(full_url)
        price_node = card.select_one('[data-marker="item-price"], meta[itemprop="price"]')
        price_text = price_node.get("content") if price_node and price_node.name == "meta" else None
        price_text = price_text or (price_node.get_text(" ", strip=True) if price_node else "")
        location_node = card.select_one('[data-marker="item-address"], [class*="geo"]')
        seller_node = card.select_one('[data-marker="seller-info/name"], [class*="seller"]')

        if not title or avito_id is None:
            continue

        ads.append(
            AvitoAd(
                avito_id=avito_id,
                title=title,
                price_rub=_digits_to_int(price_text),
                url=full_url,
                location=location_node.get_text(" ", strip=True) if location_node else None,
                seller=seller_node.get_text(" ", strip=True) if seller_node else None,
                parsed_at=parsed_at,
            )
        )

    if not ads:
        raise AvitoParserError(f"No ads parsed from {source_url}. Avito may have changed markup or blocked the request.")
    return ads


def parse_avito_search(config: AvitoConfig) -> list[AvitoAd]:
    headers = {"User-Agent": config.user_agent, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    result: list[AvitoAd] = []

    with requests.Session() as session:
        for url in config.urls:
            for page in range(1, config.max_pages + 1):
                page_url = url if page == 1 else f"{url}{'&' if '?' in url else '?'}p={page}"
                response = session.get(page_url, headers=headers, timeout=config.request_timeout)
                if response.status_code >= 400:
                    raise AvitoParserError(f"Avito returned HTTP {response.status_code} for {page_url}")
                result.extend(parse_avito_html(response.text, page_url))

    unique: dict[int, AvitoAd] = {}
    for ad in result:
        unique[ad.avito_id] = ad
    return list(unique.values())

