-- These tables' size on disk, indexes included, largest first.
SELECT relname, pg_total_relation_size(oid)
FROM pg_class
WHERE relkind = 'r' AND relnamespace = current_schema()::regnamespace
  AND relname = ANY(%(tables)s)
ORDER BY 2 DESC, 1
