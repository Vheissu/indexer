-- Each URL keeps its most recent successful check, however old. Its other successful checks go,
-- and so do failed checks older than the cutoff.
DELETE FROM health_checks h
WHERE CASE
    WHEN h.error IS NULL THEN EXISTS (
        SELECT 1 FROM health_checks n
        WHERE n.url = h.url AND n.error IS NULL AND (n.checked_at, n.id) > (h.checked_at, h.id)
    )
    ELSE h.checked_at < now() - make_interval(months => %(months)s)
END
