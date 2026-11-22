-- Recent posts are only kept for a few days: they are the bulk of a check's size.
UPDATE health_checks SET recent_posts = NULL
WHERE recent_posts IS NOT NULL AND checked_at < now() - make_interval(days => %(days)s)
