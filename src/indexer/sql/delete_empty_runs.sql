-- Runs older than the cutoff that have no checks left.
DELETE FROM runs r
WHERE r.started_at < now() - make_interval(months => %(months)s)
  AND NOT EXISTS (SELECT 1 FROM health_checks h WHERE h.run_id = r.id)
