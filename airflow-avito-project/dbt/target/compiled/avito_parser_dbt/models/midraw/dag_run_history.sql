

with raw_rows as (
    select
        run_id,
        row_number,
        source_url,
        csv_row,
        loaded_at
    from "avito_dwh"."raw"."avito_ads_csv"
    where csv_header = 'avito_id,title,price_rub,url,location,seller,parsed_at'
    
)

select
    run_id,
    nullif(split_part(csv_row, ',', 1), '')::bigint as avito_id,
    row_number,
    loaded_at,
    case
        when source_url like 'synthetic://%' then 'synthetic_avito'
        else 'avito'
    end as source_system
from raw_rows