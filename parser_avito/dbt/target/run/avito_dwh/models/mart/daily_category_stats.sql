
  create view "raw_dwh"."mart"."daily_category_stats__dbt_tmp"
    
    
  as (
    -- Витрина: ежедневная статистика по категориям
-- core.normalized_ads → mart.daily_category_stats

SELECT
    DATE(last_seen) AS date,
    category_auto,
    COUNT(*) AS active_ads,
    ROUND(AVG(price)) AS avg_price,
    MIN(price) AS min_price,
    MAX(price) AS max_price,
    COUNT(DISTINCT seller) AS unique_sellers,
    SUM(total_views) AS total_views
FROM "raw_dwh"."core"."normalized_ads"
WHERE is_active = TRUE
GROUP BY DATE(last_seen), category_auto
ORDER BY date DESC, category_auto
  );