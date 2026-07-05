{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name | default(target.schema, true) }}
{%- endmacro %}