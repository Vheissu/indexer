-- The table name is filled in, quoted, by psycopg.sql.
DELETE FROM {table} WHERE url = ANY(%(urls)s) RETURNING url
