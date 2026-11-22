-- Which of the given tables exist in the current schema (not in other schemas on the path).
SELECT tablename FROM pg_tables
WHERE schemaname = current_schema() AND tablename = ANY(%(tables)s)
ORDER BY tablename
