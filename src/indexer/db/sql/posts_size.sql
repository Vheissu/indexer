-- How many checks hold recent posts, and the bytes those posts take as stored.
SELECT count(recent_posts), coalesce(sum(pg_column_size(recent_posts)), 0)::bigint
FROM health_checks
