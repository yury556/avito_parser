from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone


CSV_COLUMNS = [
    "avito_id",
    "title",
    "price_rub",
    "url",
    "location",
    "seller",
    "description",
    "parsed_at",
]


@dataclass(frozen=True)
class AvitoAd:
    avito_id: int
    title: str
    price_rub: int | None
    url: str
    location: str | None = None
    seller: str | None = None
    description: str | None = None
    parsed_at: datetime | None = None


def csv_header() -> str:
    return ",".join(CSV_COLUMNS)


def _clean_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    return " ".join(text.replace(",", " ").replace('"', "'").split())


def ad_to_csv_row(ad: AvitoAd) -> str:
    parsed_at = ad.parsed_at or datetime.now(timezone.utc)
    values = [
        ad.avito_id,
        ad.title,
        ad.price_rub if ad.price_rub is not None else "",
        ad.url,
        ad.location or "",
        ad.seller or "",
        ad.description or "",
        parsed_at.isoformat(),
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="")
    writer.writerow([_clean_cell(value) for value in values])
    return buffer.getvalue()


def ads_to_csv_rows(ads: list[AvitoAd]) -> list[str]:
    return [ad_to_csv_row(ad) for ad in ads]
