from __future__ import annotations

import os
from dataclasses import dataclass


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
    max_pages: int
    request_timeout: int
    user_agent: str


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
    return AvitoConfig(
        urls=urls,
        max_pages=max(1, int(os.getenv("AVITO_MAX_PAGES", "1"))),
        request_timeout=max(1, int(os.getenv("AVITO_REQUEST_TIMEOUT", "20"))),
        user_agent=os.getenv(
            "AVITO_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        ),
    )

