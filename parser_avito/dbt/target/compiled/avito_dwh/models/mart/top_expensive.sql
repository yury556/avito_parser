-- Топ-10 самых дорогих активных объявлений
SELECT
    avito_id,
    title,
    price,
    category_auto,
    brand_auto,
    price_tier,
    seller,
    location,
    total_views,
    last_seen
FROM "raw_dwh"."core"."normalized_ads"
WHERE is_active = TRUE
ORDER BY price DESC
LIMIT 10