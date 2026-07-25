
    
    select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
  
    
    



select row_number
from "avito_dwh"."midraw"."avito_ads"
where row_number is null



  
  
      
    ) dbt_internal_test