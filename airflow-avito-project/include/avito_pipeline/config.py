from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PostgresConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str


@dataclass(frozen=True)
class AvitoConfig:
    urls: list[str]
    max_pages: int = 1
    request_timeout: int = 20
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
    proxy_string: str | None = None
    proxy_change_url: str | None = None
    cookies_api_key: str | None = None
    use_bypass_api: bool = False
    use_own_cookies: bool = False
    min_price: int = 0
    max_price: int = 999_999_999
    white_list: list[str] = field(default_factory=list)
    black_list: list[str] = field(default_factory=list)
    seller_black_list: list[str] = field(default_factory=list)
    max_age: int = 86400
    ignore_reserv: bool = True
    ignore_promotion: bool = False
    parse_views: bool = False
    retry_delay: int = 5
    block_threshold: int = 3
    pause_between_links: int = 5


def postgres_config_from_env() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("POSTGRES_HOST", "external-postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "avito_dwh"),
        user=os.getenv("POSTGRES_USER", "avito"),
        password=os.getenv("POSTGRES_PASSWORD", "avito123"),
    )


def avito_config_from_env() -> AvitoConfig:
    urls_raw = os.getenv("AVITO_SEARCH_URLS", "https://www.avito.ru/moskva?q=iphone")
    urls = [url.strip() for url in urls_raw.split(";") if url.strip()]

    white_raw = os.getenv("AVITO_WHITE_LIST", "")
    black_raw = os.getenv("AVITO_BLACK_LIST", "")
    seller_black_raw = os.getenv("AVITO_SELLER_BLACK_LIST", "")

    return AvitoConfig(
        urls=urls,
        max_pages=max(1, int(os.getenv("AVITO_MAX_PAGES", "1"))),
        request_timeout=max(1, int(os.getenv("AVITO_REQUEST_TIMEOUT", "20"))),
        user_agent=os.getenv(
            "AVITO_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        ),
        proxy_string=os.getenv("AVITO_PROXY_STRING"),
        proxy_change_url=os.getenv("AVITO_PROXY_CHANGE_URL"),
        cookies_api_key=os.getenv("AVITO_COOKIES_API_KEY"),
        use_bypass_api=os.getenv("AVITO_USE_BYPASS_API", "false").lower() == "true",
        use_own_cookies=os.getenv("AVITO_USE_OWN_COOKIES", "false").lower() == "true",
        min_price=int(os.getenv("AVITO_MIN_PRICE", "0")),
        max_price=int(os.getenv("AVITO_MAX_PRICE", "999999999")),
        white_list=[w.strip() for w in white_raw.split(",") if w.strip()],
        black_list=[b.strip() for b in black_raw.split(",") if b.strip()],
        seller_black_list=[s.strip() for s in seller_black_raw.split(",") if s.strip()],
        max_age=int(os.getenv("AVITO_MAX_AGE", "86400")),
        ignore_reserv=os.getenv("AVITO_IGNORE_RESERV", "true").lower() == "true",
        ignore_promotion=os.getenv("AVITO_IGNORE_PROMOTION", "false").lower() == "true",
        parse_views=os.getenv("AVITO_PARSE_VIEWS", "false").lower() == "true",
        retry_delay=int(os.getenv("AVITO_RETRY_DELAY", "5")),
        block_threshold=int(os.getenv("AVITO_BLOCK_THRESHOLD", "3")),
        pause_between_links=int(os.getenv("AVITO_PAUSE_BETWEEN_LINKS", "5")),
    )