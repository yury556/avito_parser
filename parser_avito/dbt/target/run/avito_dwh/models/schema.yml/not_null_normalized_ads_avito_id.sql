
    select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
  
    
    



select avito_id
from "raw_dwh"."core"."normalized_ads"
where avito_id is null



  
  
      
    ) dbt_internal_test