
    
    select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
  
    
    



select avito_id
from "avito_dwh"."midraw"."avito_ads"
where avito_id is null



  
  
      
    ) dbt_internal_test