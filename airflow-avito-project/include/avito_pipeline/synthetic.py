from __future__ import annotations

from datetime import datetime, timezone

from avito_pipeline.csv_contract import AvitoAd


def synthetic_ads() -> list[AvitoAd]:
    parsed_at = datetime.now(timezone.utc)
    return [
        AvitoAd(
            avito_id=900000001,
            title="iPhone 14 Pro 256GB sample",
            price_rub=72000,
            url="https://www.avito.ru/sample/iphone_14_pro_900000001",
            location="Москва",
            seller="sample_private",
            parsed_at=parsed_at,
        ),
        AvitoAd(
            avito_id=900000002,
            title="MacBook Air M2 sample",
            price_rub=88000,
            url="https://www.avito.ru/sample/macbook_air_m2_900000002",
            location="Санкт-Петербург",
            seller="sample_shop",
            parsed_at=parsed_at,
        ),
        AvitoAd(
            avito_id=900000003,
            title="Samsung Galaxy S24 sample",
            price_rub=54000,
            url="https://www.avito.ru/sample/samsung_s24_900000003",
            location="Казань",
            seller="sample_private",
            parsed_at=parsed_at,
        ),
    ]

