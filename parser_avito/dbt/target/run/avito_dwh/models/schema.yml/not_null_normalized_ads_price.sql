
    select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
  
    
    



select price
from "raw_dwh"."core"."normalized_ads"
where price is null



  
  
      
    ) dbt_internal_test