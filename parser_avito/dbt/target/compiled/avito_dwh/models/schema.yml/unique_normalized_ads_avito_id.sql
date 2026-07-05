
    
    

select
    avito_id as unique_field,
    count(*) as n_records

from "raw_dwh"."core"."normalized_ads"
where avito_id is not null
group by avito_id
having count(*) > 1


