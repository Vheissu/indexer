"""Postgres reads and writes, in SQL through psycopg. Liquibase owns the schema (db/changelog);
this module never creates it. The tables are described in models.py."""

import asyncio
import ipaddress
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Self

import psycopg
from psycopg import sql
from psycopg.rows import class_row

from indexer.check.runner import CheckRow
from indexer.export import ListedFeed
from indexer.models import Feed, HealthCheck, Input, IPAddress, Model, Run, columns, values
from indexer.sources import InputRow

DEFAULT_DATABASE_URL = "postgresql://indexer@127.0.0.1:5432/indexer"  # password: $PGPASSWORD
# Every table this project creates (db/changelog), plus Liquibase's own: what drop-tables drops.
OWN_TABLES = (
    "health_checks",
    "runs",
    "feeds",
    "inputs",
    "databasechangelog",
    "databasechangeloglock",
)


def _insert_sql(model: Model, replace_on: str | None) -> sql.Composed:
    """INSERT INTO <table> (<writable columns>) VALUES (%(column)s, ...)
    [ON CONFLICT (<replace_on>) DO UPDATE SET <column> = EXCLUDED.<column>, ...] RETURNING *"""
    cols = columns(model, writable=True)
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(model.TABLE),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(map(sql.Placeholder, cols)),
    )
    if replace_on is not None:
        stmt += sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
            sql.Identifier(replace_on),
            sql.SQL(", ").join(
                sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c))
                for c in cols
                if c != replace_on
            ),
        )
    return stmt + sql.SQL(" RETURNING *")


# The columns a CheckRow fills under the same name.
_CHECK_FIELDS = [
    c for c in columns(HealthCheck, writable=True) if c not in ("run_id", "ip_address")
]
FEED_METADATA = [c for c in columns(Feed) if c not in ("url", "updated_at", "parsed_at")]


def _inet(value: str | None) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value.split("%")[0]) if value else None
    except ValueError:
        return None


# Insert new URLs; for a URL already present, replace its source only when the new license is
# strictly more permissive (lower tier). Unknown licenses rank last. first_seen_at and
# last_checked_at are never touched here.
_UPSERT_INPUTS = """
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
"""


def _health_check(run_id: int, row: CheckRow) -> HealthCheck:
    fields = {name: getattr(row, name) for name in _CHECK_FIELDS}
    return HealthCheck(run_id=run_id, ip_address=_inet(row.ip_address), **fields)


def _feed(row: CheckRow) -> Feed:
    metadata = {name: getattr(row, name) for name in FEED_METADATA}
    return Feed(url=row.url, updated_at=row.checked_at, parsed_at=row.checked_at, **metadata)


# Tables `undo` deletes a URL from, in deletion order.
UNDO_TABLES = ("health_checks", "feeds", "inputs")


class Database:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn
        # One connection serves every concurrent check. A connection runs one transaction at a
        # time, so concurrent writers must take turns; otherwise their transactions interleave
        # and psycopg aborts them (OutOfOrderTransactionNesting).
        self._lock = asyncio.Lock()

    @classmethod
    async def connect(cls, dsn: str) -> Self:
        return cls(await psycopg.AsyncConnection.connect(dsn, autocommit=True))

    async def close(self) -> None:
        await self._conn.close()

    async def _insert[R: (Run, HealthCheck, Input, Feed)](
        self, row: R, *, replace_on: str | None = None
    ) -> R:
        """Store `row` and return it as stored, with the columns the database fills (ids,
        defaults). With `replace_on`, a row that already has that column's value is replaced."""
        cur = self._conn.cursor(row_factory=class_row(type(row)))
        await cur.execute(_insert_sql(type(row), replace_on), values(row))
        stored = await cur.fetchone()
        assert stored is not None
        return stored

    async def start_run(self) -> int:
        return (await self._insert(Run(started_at=datetime.now(UTC)))).id

    async def finish_run(self, run_id: int) -> None:
        await self._conn.execute(
            "UPDATE runs SET finished_at = %s WHERE id = %s", (datetime.now(UTC), run_id)
        )

    async def select_batch(self, size: int, stale_days: int) -> list[str]:
        """A uniformly random sample of inputs never checked, or last checked more than
        `stale_days` ago. Each run is a fresh random draw; a URL checked recently is excluded,
        so repeated runs work through every URL before revisiting any."""
        cur = await self._conn.execute(
            "SELECT url FROM inputs"
            " WHERE last_checked_at IS NULL"
            "    OR last_checked_at < now() - make_interval(days => %s)"
            " ORDER BY random() LIMIT %s",
            (stale_days, size),
        )
        return [row[0] for row in await cur.fetchall()]

    async def known_inputs(self, urls: Sequence[str]) -> set[str]:
        """Those of `urls` that are already inputs."""
        cur = await self._conn.execute("SELECT url FROM inputs WHERE url = ANY(%s)", (list(urls),))
        return {row[0] for row in await cur.fetchall()}

    async def insert_check(self, run_id: int, row: CheckRow) -> None:
        """Store one check, in one transaction:
        - the per-check health row;
        - the feed's metadata in `feeds`: replaced when the feed parsed, untouched when the
          check failed;
        - `inputs.last_checked_at`, if the URL is an input (any outcome: a failing feed waits
          its turn like the rest)."""
        async with self._lock, self._conn.transaction():
            await self._insert(_health_check(run_id, row))
            if row.error is None:
                # Descriptive metadata lives once per feed, not on every check. A parsed feed
                # replaces all of it: a field the feed dropped becomes null.
                await self._insert(_feed(row), replace_on="url")
            await self._conn.execute(
                "UPDATE inputs SET last_checked_at = %s WHERE url = %s",
                (row.checked_at, row.url),
            )

    async def upsert_inputs(
        self, rows: Sequence[InputRow], license_tiers: Mapping[str, int]
    ) -> tuple[int, int]:
        """Load rows into `inputs` in one transaction. Returns (inserted, re-attributed)."""
        licenses = list(license_tiers)
        params = {"licenses": licenses, "tiers": [license_tiers[x] for x in licenses]}
        async with self._lock, self._conn.transaction(), self._conn.cursor() as cur:
            await cur.execute(
                "CREATE TEMP TABLE inputs_load (url text, source text, source_license text)"
                " ON COMMIT DROP"
            )
            async with cur.copy(
                "COPY inputs_load (url, source, source_license) FROM STDIN"
            ) as copy:
                for row in rows:
                    await copy.write_row((row.url, row.source, row.source_license))
            await cur.execute(_UPSERT_INPUTS, params)
            changed = [r[0] for r in await cur.fetchall()]
        inserted = sum(changed)
        return inserted, len(changed) - inserted

    async def listed_feeds(self, success_days: int, item_years: int) -> list[ListedFeed]:
        """Feeds with a successful check in the last `success_days` days whose newest item, as
        of their latest successful check, is less than `item_years` years old."""
        cur = self._conn.cursor(row_factory=class_row(ListedFeed))
        await cur.execute(
            """
            WITH latest_ok AS (
                SELECT DISTINCT ON (url) url, last_item_date, file_size
                FROM health_checks
                WHERE error IS NULL AND checked_at > now() - make_interval(days => %s)
                ORDER BY url, checked_at DESC
            )
            SELECT f.url, f.title, f.site_url, l.last_item_date, f.language, l.file_size, f.topics,
                   f.subtitle, i.source, i.source_license
            FROM feeds f JOIN latest_ok l USING (url) LEFT JOIN inputs i USING (url)
            WHERE l.last_item_date > now() - make_interval(years => %s)
            """,
            (success_days, item_years),
        )
        return await cur.fetchall()

    async def drop_all_tables(self) -> list[str]:
        """Drop every table this project owns, Liquibase's bookkeeping included, so the next
        migration rebuilds an empty schema. Only the tables named in OWN_TABLES: other tables in
        the database (Supabase's, another app's) are never touched. Returns those that existed."""
        async with self._lock, self._conn.transaction():
            cur = await self._conn.execute(
                "SELECT tablename FROM pg_tables"
                " WHERE schemaname = current_schema() AND tablename = ANY(%s) ORDER BY tablename",
                (list(OWN_TABLES),),
            )
            existing = [row[0] for row in await cur.fetchall()]
            if existing:
                await self._conn.execute(
                    sql.SQL("DROP TABLE {} CASCADE").format(
                        sql.SQL(", ").join(sql.Identifier(t) for t in existing)
                    )
                )
            return existing

    async def delete_urls(self, urls: Sequence[str]) -> dict[str, dict[str, int]]:
        """Delete these exact URLs from health_checks, feeds and inputs, in one transaction.
        Returns, per URL that was found anywhere, the rows deleted from each table."""
        found: dict[str, dict[str, int]] = {}
        async with self._lock, self._conn.transaction():
            for table in UNDO_TABLES:
                cur = await self._conn.execute(
                    sql.SQL("DELETE FROM {} WHERE url = ANY(%s) RETURNING url").format(
                        sql.Identifier(table)
                    ),
                    (list(urls),),
                )
                for (url,) in await cur.fetchall():
                    counts = found.setdefault(url, dict.fromkeys(UNDO_TABLES, 0))
                    counts[table] += 1
        return found

    async def delete_old_checks(self, months: int) -> tuple[int, int]:
        """Delete health checks older than `months`, then runs that are older than that and
        have no checks left. feeds and inputs are untouched. Returns (checks, runs) deleted."""
        async with self._lock, self._conn.transaction():
            cur = await self._conn.execute(
                "DELETE FROM health_checks WHERE checked_at < now() - make_interval(months => %s)",
                (months,),
            )
            checks = cur.rowcount
            cur = await self._conn.execute(
                "DELETE FROM runs r WHERE r.started_at < now() - make_interval(months => %s)"
                " AND NOT EXISTS (SELECT 1 FROM health_checks h WHERE h.run_id = r.id)",
                (months,),
            )
            return checks, cur.rowcount
