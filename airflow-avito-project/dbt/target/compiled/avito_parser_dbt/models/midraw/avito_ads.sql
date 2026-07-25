

with parsed as (
    select
        nullif(split_part(csv_row, ',', 1), '')::bigint as avito_id,
        nullif(split_part(csv_row, ',', 2), '') as title,
        nullif(split_part(csv_row, ',', 3), '')::numeric(12, 2) as price_rub,
        nullif(split_part(csv_row, ',', 4), '') as url,
        nullif(split_part(csv_row, ',', 5), '') as location,
        nullif(split_part(csv_row, ',', 6), '') as seller,
        nullif(split_part(csv_row, ',', 7), '')::timestamptz as parsed_at,
        source_url
    from "avito_dwh"."raw"."avito_ads_csv"
    where csv_header = 'avito_id,title,price_rub,url,location,seller,parsed_at'
)

select
    avito_id,
    title,
    price_rub,
    url,
    location,
    seller,
    parsed_at,
    source_url,
    case
        when source_url like 'synthetic://%' then 'synthetic_avito'
        else 'avito'
    end as source_system
from parsed