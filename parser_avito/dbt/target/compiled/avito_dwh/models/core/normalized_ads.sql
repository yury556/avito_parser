-- Первая dbt-модель: нормализованные объявления из сырого слоя
-- raw.ads → core.normalized_ads

SELECT
    avito_id,
    title,
    price,

    -- Категория на основе названия (грубая классификация)
    CASE
        WHEN title ILIKE '%rtx%' OR title ILIKE '%gtx%' OR title ILIKE '%radeon%' OR title ILIKE '%видеокарт%' THEN 'videocard'
        WHEN title ILIKE '%i5%' OR title ILIKE '%i7%' OR title ILIKE '%i9%' OR title ILIKE '%ryzen%' OR title ILIKE '%процессор%' THEN 'cpu'
        WHEN title ILIKE '%ddr%' OR title ILIKE '%ram%' OR title ILIKE '%оператив%' OR title ILIKE '%озу%' THEN 'ram'
        WHEN title ILIKE '%ssd%' OR title ILIKE '%nvme%' OR title ILIKE '%накопител%' THEN 'storage'
        WHEN title ILIKE '%материнск%' OR title ILIKE '%motherboard%' OR title ILIKE '%b650%' OR title ILIKE '%z790%' THEN 'motherboard'
        WHEN title ILIKE '%блок пита%' OR title ILIKE '%psu%' OR title ILIKE '%power%' THEN 'psu'
        WHEN title ILIKE '%телефон%' OR title ILIKE '%iphone%' OR title ILIKE '%samsung%' OR title ILIKE '%смартфон%' THEN 'phone'
        ELSE 'other'
    END AS category_auto,

    -- Бренд из названия
    CASE
        WHEN title ILIKE '%nvidia%' OR title ILIKE '%geforce%' THEN 'nvidia'
        WHEN title ILIKE '%amd%' OR title ILIKE '%radeon%' THEN 'amd'
        WHEN title ILIKE '%intel%' THEN 'intel'
        WHEN title ILIKE '%gigabyte%' THEN 'gigabyte'
        WHEN title ILIKE '%msi%' THEN 'msi'
        WHEN title ILIKE '%asus%' THEN 'asus'
        WHEN title ILIKE '%samsung%' THEN 'samsung'
        WHEN title ILIKE '%apple%' OR title ILIKE '%iphone%' THEN 'apple'
        ELSE 'unknown'
    END AS brand_auto,

    -- Ценовая категория
    CASE
        WHEN price < 5000 THEN 'budget'
        WHEN price < 20000 THEN 'medium'
        WHEN price < 50000 THEN 'premium'
        WHEN price >= 50000 THEN 'flagship'
        ELSE 'unknown'
    END AS price_tier,

    seller,
    location,
    total_views,
    today_views,
    is_promoted,
    is_active,
    first_seen,
    last_seen,
    CURRENT_TIMESTAMP AS transformed_at

FROM "raw_dwh"."raw"."ads"