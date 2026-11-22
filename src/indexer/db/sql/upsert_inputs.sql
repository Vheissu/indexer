-- Insert new URLs; for a URL already present, replace its source only when the new license is
-- strictly more permissive (lower tier). Unknown licenses rank last. first_seen_at and
-- last_checked_at are never touched here. Postgres sets xmax to 0 on a freshly inserted row.
INSERT INTO inputs AS i (url, source, source_license)
SELECT url, source, source_license FROM inputs_load
ON CONFLICT (url) DO UPDATE
SET source = EXCLUDED.source, source_license = EXCLUDED.source_license
WHERE coalesce(
          (%(tiers)s::int[])[array_position(%(licenses)s::text[], EXCLUDED.source_license)],
          2147483647)
    < coalesce(
          (%(tiers)s::int[])[array_position(%(licenses)s::text[], i.source_license)],
          2147483647)
RETURNING (xmax::text = '0') AS inserted
