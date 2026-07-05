
    select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
  
    
    

select
    avito_id as unique_field,
    count(*) as n_records

from "raw_dwh"."core"."normalized_ads"
where avito_id is not null
group by avito_id
having count(*) > 1



  
  
      
    ) dbt_internal_test