-- A uniformly random sample of inputs never checked, or last checked over stale_days ago.
SELECT url FROM inputs
WHERE last_checked_at IS NULL
   OR last_checked_at < now() - make_interval(days => %(stale_days)s)
ORDER BY random()
LIMIT %(size)s
