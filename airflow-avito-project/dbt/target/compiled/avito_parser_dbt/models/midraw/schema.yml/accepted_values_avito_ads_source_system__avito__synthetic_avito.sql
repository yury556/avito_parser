
    
    

with all_values as (

    select
        source_system as value_field,
        count(*) as n_records

    from "avito_dwh"."midraw"."avito_ads"
    group by source_system

)

select *
from all_values
where value_field not in (
    'avito','synthetic_avito'
)


