-- Any outcome: a failing feed waits its turn like the rest. No row if the URL is not an input.
UPDATE inputs SET last_checked_at = %(checked_at)s WHERE url = %(url)s
