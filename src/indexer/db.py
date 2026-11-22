"""Postgres reads and writes, through SQLAlchemy. Liquibase owns the schema (db/changelog); this
module never creates it. The tables are described in models.py."""

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Self

import psycopg
from sqlalchemy import ColumnElement, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from indexer.check.runner import CheckRow
from indexer.export import ListedFeed
from indexer.models import Feed, HealthCheck, Input, IPAddress, Run
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

# Columns filled from a CheckRow of the same name.
_CHECK_FIELDS = [c.name for c in HealthCheck.__table__.columns if c.name not in ("id", "run_id")]
# Descriptive metadata lives once per feed (feeds), not on every check. A parsed feed
# overwrites all of it (a field the feed dropped becomes null).
FEED_METADATA = [
    c.name for c in Feed.__table__.columns if c.name not in ("url", "updated_at", "parsed_at")
]

# Tables `undo` deletes a URL from, in deletion order.
UNDO_TABLES = ("health_checks", "feeds", "inputs")
_UNDO_MODELS = (HealthCheck, Feed, Input)


def _inet(value: str | None) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value.split("%")[0]) if value else None
    except ValueError:
        return None


def _ago(*, years: int = 0, months: int = 0, days: int = 0) -> ColumnElement[datetime]:
    """now() minus an interval, computed by Postgres."""
    return func.now() - func.make_interval(years, months, 0, days)


# Insert new URLs; for a URL already present, replace its source only when the new license is
# strictly more permissive (lower tier). Unknown licenses rank last. first_seen_at and
# last_checked_at are never touched here. Postgres sets xmax to 0 on a freshly inserted row.
_UPSERT_INPUTS = text("""
INSERT INTO inputs AS i (url, source, source_license)
SELECT url, source, source_license FROM inputs_load
ON CONFLICT (url) DO UPDATE
SET source = EXCLUDED.source, source_license = EXCLUDED.source_license
WHERE coalesce(
          (CAST(:tiers AS int[]))[
              array_position(CAST(:licenses AS text[]), EXCLUDED.source_license)],
          2147483647)
    < coalesce(
          (CAST(:tiers AS int[]))[
              array_position(CAST(:licenses AS text[]), i.source_license)],
          2147483647)
RETURNING (xmax::text = '0') AS inserted
""")

# Feeds whose latest successful check is recent enough and whose newest item, as of that check,
# is young enough.
_LISTED_FEEDS = text("""
WITH latest_ok AS (
    SELECT DISTINCT ON (url) url, last_item_date, file_size
    FROM health_checks
    WHERE error IS NULL AND checked_at > now() - make_interval(days => :success_days)
    ORDER BY url, checked_at DESC
)
SELECT f.url, f.title, f.site_url, l.last_item_date, f.language, l.file_size, f.topics,
       f.subtitle, i.source, i.source_license
FROM feeds f JOIN latest_ok l USING (url) LEFT JOIN inputs i USING (url)
WHERE l.last_item_date > now() - make_interval(years => :item_years)
""")


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        # One connection serves every concurrent check. A connection runs one transaction at a
        # time, so concurrent writers must take turns; otherwise their transactions interleave
        # and psycopg aborts them (OutOfOrderTransactionNesting).
        self._lock = asyncio.Lock()

    @classmethod
    async def connect(cls, dsn: str) -> Self:
        # psycopg opens the connection, so `dsn` may be a URL or a libpq keyword string (CI).
        engine = create_async_engine(
            "postgresql+psycopg://",
            async_creator=lambda: psycopg.AsyncConnection.connect(dsn),
            pool_size=1,
            max_overflow=0,
        )
        return cls(engine)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        """The connection, in a transaction, once the other writers are done with it."""
        async with self._lock, self._engine.begin() as conn:
            yield conn

    async def close(self) -> None:
        await self._engine.dispose()

    async def start_run(self) -> int:
        async with self._transaction() as conn:
            stmt = insert(Run).values(started_at=datetime.now(UTC)).returning(Run.id)
            return (await conn.execute(stmt)).scalar_one()

    async def finish_run(self, run_id: int) -> None:
        async with self._transaction() as conn:
            stmt = update(Run).where(Run.id == run_id).values(finished_at=datetime.now(UTC))
            await conn.execute(stmt)

    async def select_batch(self, size: int, stale_days: int) -> list[str]:
        """A uniformly random sample of inputs never checked, or last checked more than
        `stale_days` ago. Each run is a fresh random draw; a URL checked recently is excluded,
        so repeated runs work through every URL before revisiting any."""
        stmt = (
            select(Input.url)
            .where(
                or_(Input.last_checked_at.is_(None), Input.last_checked_at < _ago(days=stale_days))
            )
            .order_by(func.random())
            .limit(size)
        )
        async with self._transaction() as conn:
            return list((await conn.scalars(stmt)).all())

    async def known_inputs(self, urls: Sequence[str]) -> set[str]:
        """Those of `urls` that are already inputs."""
        async with self._transaction() as conn:
            return set(await conn.scalars(select(Input.url).where(Input.url.in_(urls))))

    async def insert_check(self, run_id: int, row: CheckRow) -> None:
        """Store one check, in one transaction:
        - the per-check health row;
        - the feed's metadata in `feeds`: replaced when the feed parsed, untouched when the
          check failed;
        - `inputs.last_checked_at`, if the URL is an input (any outcome: a failing feed waits
          its turn like the rest)."""
        check = {name: getattr(row, name) for name in _CHECK_FIELDS}
        check.update(run_id=run_id, ip_address=_inet(row.ip_address))
        async with self._transaction() as conn:
            await conn.execute(insert(HealthCheck).values(check))
            if row.error is None:
                metadata = {name: getattr(row, name) for name in FEED_METADATA}
                times = {"updated_at": row.checked_at, "parsed_at": row.checked_at}
                upsert = pg_insert(Feed).values(url=row.url, **times, **metadata)
                replace = {name: upsert.excluded[name] for name in times | metadata}
                await conn.execute(
                    upsert.on_conflict_do_update(index_elements=[Feed.url], set_=replace)
                )
            await conn.execute(
                update(Input).where(Input.url == row.url).values(last_checked_at=row.checked_at)
            )

    async def upsert_inputs(
        self, rows: Sequence[InputRow], license_tiers: Mapping[str, int]
    ) -> tuple[int, int]:
        """Load rows into `inputs` in one transaction. Returns (inserted, re-attributed).

        A new URL is inserted. A URL already present moves to the new source only when the new
        license is strictly more permissive (a lower tier). first_seen_at and last_checked_at
        are never touched here."""
        licenses = list(license_tiers)
        params = {"licenses": licenses, "tiers": [license_tiers[x] for x in licenses]}
        async with self._transaction() as conn:
            # COPY, which SQLAlchemy lacks, through psycopg's own connection.
            raw = (await conn.get_raw_connection()).driver_connection
            assert isinstance(raw, psycopg.AsyncConnection)
            async with raw.cursor() as cur:
                await cur.execute(
                    "CREATE TEMP TABLE inputs_load (url text, source text, source_license text)"
                    " ON COMMIT DROP"
                )
                async with cur.copy(
                    "COPY inputs_load (url, source, source_license) FROM STDIN"
                ) as copy:
                    for row in rows:
                        await copy.write_row((row.url, row.source, row.source_license))
            changed = list((await conn.execute(_UPSERT_INPUTS, params)).scalars())
        inserted = sum(changed)
        return inserted, len(changed) - inserted

    async def listed_feeds(self, success_days: int, item_years: int) -> list[ListedFeed]:
        """Feeds with a successful check in the last `success_days` days whose newest item, as
        of their latest successful check, is less than `item_years` years old."""
        params = {"success_days": success_days, "item_years": item_years}
        async with self._transaction() as conn:
            return [ListedFeed(*row) for row in await conn.execute(_LISTED_FEEDS, params)]

    async def drop_all_tables(self) -> list[str]:
        """Drop every table this project owns, Liquibase's bookkeeping included, so the next
        migration rebuilds an empty schema. Only the tables named in OWN_TABLES: other tables in
        the database (Supabase's, another app's) are never touched. Returns those that existed."""
        async with self._transaction() as conn:
            existing = list(
                await conn.scalars(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                        " AND tablename = ANY(:tables) ORDER BY tablename"
                    ),
                    {"tables": list(OWN_TABLES)},
                )
            )
            if existing:
                quote = conn.dialect.identifier_preparer.quote
                await conn.exec_driver_sql(f"DROP TABLE {', '.join(map(quote, existing))} CASCADE")
            return existing

    async def delete_old_checks(self, months: int) -> tuple[int, int]:
        """Delete health checks older than `months`, then runs that are older than that and
        have no checks left. feeds and inputs are untouched. Returns (checks, runs) deleted."""
        cutoff = _ago(months=months)
        async with self._transaction() as conn:
            checks = await conn.execute(delete(HealthCheck).where(HealthCheck.checked_at < cutoff))
            has_checks = select(HealthCheck.id).where(HealthCheck.run_id == Run.id).exists()
            runs = await conn.execute(delete(Run).where(Run.started_at < cutoff, ~has_checks))
            return checks.rowcount, runs.rowcount

    async def delete_urls(self, urls: Sequence[str]) -> dict[str, dict[str, int]]:
        """Delete these exact URLs from health_checks, feeds and inputs, in one transaction.
        Returns, per URL that was found anywhere, the rows deleted from each table."""
        found: dict[str, dict[str, int]] = {}
        async with self._transaction() as conn:
            for model in _UNDO_MODELS:
                stmt = delete(model).where(model.url.in_(urls)).returning(model.url)
                for url in await conn.scalars(stmt):
                    counts = found.setdefault(url, dict.fromkeys(UNDO_TABLES, 0))
                    counts[model.__tablename__] += 1
        return found
