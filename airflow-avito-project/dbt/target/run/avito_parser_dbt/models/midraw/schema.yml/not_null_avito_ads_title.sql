
    
    select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
  
    
    



select title
from "avito_dwh"."midraw"."avito_ads"
where title is null



  
  
      
    ) dbt_internal_test