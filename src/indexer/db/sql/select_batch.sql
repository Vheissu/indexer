-- The most outdated inputs first: never checked, then the longest since their last check; ties
-- in random order (so a batch doesn't bunch up on one host). An input checked within the
-- cooldown is never picked.
SELECT url FROM inputs
WHERE last_checked_at IS NULL
   OR last_checked_at < now() - make_interval(mins => %(cooldown_minutes)s)
ORDER BY last_checked_at ASC NULLS FIRST, random()
LIMIT %(size)s
