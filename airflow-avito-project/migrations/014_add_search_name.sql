-- ============================================================
-- Migration 014: имя поиска во всех слоях
-- raw: avito_original.ads.source_url (URL поиска, из которого пришёл лот)
-- detail: detail.ads.search_name (человеческое имя поиска)
-- ============================================================

ALTER TABLE avito_original.ads ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE detail.ads ADD COLUMN IF NOT EXISTS search_name TEXT;
CREATE INDEX IF NOT EXISTS idx_detail_search_name ON detail.ads (search_name);

-- разовый бэкфилл из lot_source + справочника
UPDATE detail.ads d
SET search_name = su.name
FROM avito_original.lot_source ls
JOIN avito_original.search_urls su ON su.url = ls.source_url
WHERE d.avito_id = ls.avito_id
  AND d.search_name IS NULL
  AND su.name IS NOT NULL;
