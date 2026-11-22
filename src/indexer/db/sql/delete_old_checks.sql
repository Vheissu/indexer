DELETE FROM health_checks WHERE checked_at < now() - make_interval(months => %(months)s)
