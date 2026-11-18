"""Optional: runs only when INDEXER_TEST_DATABASE_URL points at a migrated database."""

import asyncio
import os
from datetime import UTC, datetime

import pytest

from indexer.check.runner import CheckRow
from indexer.db import Database
from indexer.sources import InputRow

DSN = os.environ.get("INDEXER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="INDEXER_TEST_DATABASE_URL not set")


async def test_run_and_row_round_trip() -> None:
    assert DSN is not None
    db = await Database.connect(DSN)
    try:
        run_id = await db.start_run()
        row = CheckRow(
            url="https://indexer-roundtrip.example/feed",
            checked_at=datetime.now(UTC),
            final_url="https://example.com/feed.xml",
            http_status=200,
            parent_domain="example.com",
            last_item_date=datetime(2024, 1, 1, tzinfo=UTC),
            redirected=True,
            file_size=123,
            item_count=4,
            fetch_time_ms=50,
            response_time_ms=20,
            ttl=60,
            etag='"abc"',
            last_modified="Mon, 01 Jan 2024 10:00:00 GMT",
            first_redirect_status=301,
            ip_address="2606:4700::1",
            feed_kind="rss",
            generator="WordPress 6.4",
            site_url="https://example.com/",
            self_url="https://example.com/feed.xml",
            title="Example",
            language="de, en",
            topics="rust, webassembly",
            media_type="text",
        )
        await db.insert_check(run_id, row)
        await db.finish_run(run_id)
        conn = db._conn
        cur = await conn.execute(
            "SELECT url, item_count, redirected, error, etag,"
            " first_redirect_status, host(ip_address)"
            " FROM health_checks WHERE run_id = %s",
            (run_id,),
        )
        assert await cur.fetchall() == [(row.url, 4, True, None, '"abc"', 301, "2606:4700::1")]
        cur = await conn.execute(
            "SELECT parsed_at, feed_kind, generator, title,"
            " language, topics, media_type, site_url FROM feeds WHERE url = %s",
            (row.url,),
        )
        assert await cur.fetchone() == (
            row.checked_at,
            "rss",
            "WordPress 6.4",
            "Example",
            "de, en",
            "rust, webassembly",
            "text",
            "https://example.com/",
        )
        cur = await conn.execute(
            "SELECT finished_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)
        )
        assert await cur.fetchone() == (True,)
        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url = %s", (row.url,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
    finally:
        await db.close()


async def test_feed_metadata_update_rules() -> None:
    """Parsed: replace all metadata. Failed: keep it."""
    assert DSN is not None
    url = "https://indexer-meta.example/feed"
    db = await Database.connect(DSN)
    conn = db._conn

    async def feed() -> tuple[object, ...] | None:
        cur = await conn.execute(
            "SELECT title, subtitle, language, parsed_at, updated_at FROM feeds WHERE url = %s",
            (url,),
        )
        return await cur.fetchone()

    try:
        await conn.execute("DELETE FROM feeds WHERE url = %s", (url,))
        run_id = await db.start_run()
        t1, t2, t3, t4 = (datetime(2026, 11, d, tzinfo=UTC) for d in (1, 2, 3, 4))

        async def check(row: CheckRow) -> None:
            # one row per URL per run: clear the previous step's row first
            await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
            await db.insert_check(run_id, row)

        # a failure before the feed was ever parsed creates nothing
        await check(CheckRow(url=url, checked_at=t1, error="timeout"))
        assert await feed() is None

        await check(CheckRow(url=url, checked_at=t2, title="Old", subtitle="Sub", language="en"))
        assert await feed() == ("Old", "Sub", "en", t2, t2)

        # a failed check keeps the last known metadata
        await check(CheckRow(url=url, checked_at=t3, error="http_5xx"))
        assert await feed() == ("Old", "Sub", "en", t2, t2)

        # parsed again: everything replaced, dropped fields become null
        await check(CheckRow(url=url, checked_at=t4, title="New"))
        assert await feed() == ("New", None, None, t4, t4)

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url = %s", (url,))
    finally:
        await db.close()


async def test_upsert_inputs_keeps_most_permissive() -> None:
    assert DSN is not None
    tiers = {"CC0-1.0": 0, "MIT": 1, "ISC": 1}
    url = "https://indexer-test.example/feed"
    other = "https://indexer-test.example/other"
    db = await Database.connect(DSN)
    try:
        await db._conn.execute("DELETE FROM inputs WHERE url IN (%s, %s)", (url, other))
        assert await db.upsert_inputs([InputRow(url, "src-mit", "MIT")], tiers) == (1, 0)
        # equal tier: no change; less permissive: no change
        assert await db.upsert_inputs([InputRow(url, "src-isc", "ISC")], tiers) == (0, 0)
        # more permissive: moved; plus one new row
        rows = [InputRow(url, "src-cc0", "CC0-1.0"), InputRow(other, "src-mit", "MIT")]
        assert await db.upsert_inputs(rows, tiers) == (1, 1)
        assert await db.upsert_inputs([InputRow(url, "src-mit", "MIT")], tiers) == (0, 0)
        cur = await db._conn.execute(
            "SELECT url, source, source_license, first_seen_at IS NOT NULL, last_checked_at"
            " FROM inputs WHERE url IN (%s, %s) ORDER BY url",
            (url, other),
        )
        assert await cur.fetchall() == [
            (url, "src-cc0", "CC0-1.0", True, None),
            (other, "src-mit", "MIT", True, None),
        ]
        await db._conn.execute("DELETE FROM inputs WHERE url IN (%s, %s)", (url, other))
    finally:
        await db.close()


async def test_batch_selection_and_last_checked() -> None:
    assert DSN is not None
    urls = [f"https://indexer-batch.example/{i}" for i in range(6)]
    db = await Database.connect(DSN)
    conn = db._conn
    try:
        await conn.execute("DELETE FROM inputs WHERE url LIKE 'https://indexer-batch.example/%'")
        await db.upsert_inputs([InputRow(u, "src", "MIT") for u in urls], {"MIT": 0})
        # 0, 1: checked recently (fresh); 2: checked 20 days ago (stale); 3-5: never checked
        await conn.execute(
            "UPDATE inputs SET last_checked_at = now() - interval '1 day' WHERE url = ANY(%s)",
            (urls[:2],),
        )
        await conn.execute(
            "UPDATE inputs SET last_checked_at = now() - interval '20 days' WHERE url = %s",
            (urls[2],),
        )
        mine = set(urls)
        eligible = set(urls[2:])
        picked = [u for u in await db.select_batch(100_000, 14) if u in mine]
        assert set(picked) == eligible
        # a batch is a sample of the eligible rows, without repeats
        small = await db.select_batch(2, 14)
        assert len(small) == 2 and len(set(small)) == 2
        assert not set(small) & set(urls[:2])

        # storing a check marks the input as checked, whatever the outcome
        run_id = await db.start_run()
        row = CheckRow(url=urls[3], checked_at=datetime.now(UTC), error="timeout")
        await db.insert_check(run_id, row)
        cur = await conn.execute("SELECT last_checked_at FROM inputs WHERE url = %s", (urls[3],))
        assert await cur.fetchone() == (row.checked_at,)
        assert urls[3] not in await db.select_batch(100_000, 14)
        # a URL that is not an input is stored without error
        await db.insert_check(
            run_id, CheckRow(url="https://not-an-input.example/", checked_at=row.checked_at)
        )

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM inputs WHERE url LIKE 'https://indexer-batch.example/%'")
    finally:
        await db.close()


async def test_concurrent_inserts_share_one_connection() -> None:
    """Checks finish concurrently and all write through one connection; every row must be
    stored (regression: interleaved transactions failed with OutOfOrderTransactionNesting)."""
    assert DSN is not None
    urls = [f"https://indexer-concurrent.example/{i}" for i in range(40)]
    db = await Database.connect(DSN)
    conn = db._conn
    try:
        await conn.execute("DELETE FROM inputs WHERE url = ANY(%s)", (urls,))
        await db.upsert_inputs([InputRow(u, "src", "MIT") for u in urls], {"MIT": 0})
        run_id = await db.start_run()
        now = datetime.now(UTC)
        rows = [
            CheckRow(url=u, checked_at=now, title="t")
            if i % 2
            else CheckRow(url=u, checked_at=now, error="timeout")
            for i, u in enumerate(urls)
        ]
        await asyncio.gather(*(db.insert_check(run_id, r) for r in rows))
        cur = await conn.execute("SELECT count(*) FROM health_checks WHERE run_id = %s", (run_id,))
        assert await cur.fetchone() == (40,)
        cur = await conn.execute(
            "SELECT count(*) FROM inputs WHERE url = ANY(%s) AND last_checked_at = %s", (urls, now)
        )
        assert await cur.fetchone() == (40,)
        cur = await conn.execute("SELECT count(*) FROM feeds WHERE url = ANY(%s)", (urls,))
        assert await cur.fetchone() == (20,)

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url = ANY(%s)", (urls,))
        await conn.execute("DELETE FROM inputs WHERE url = ANY(%s)", (urls,))
    finally:
        await db.close()


async def test_listed_feeds_rules() -> None:
    """Listed: a successful check within success_days, newest item younger than item_years."""
    assert DSN is not None
    base = "https://indexer-listed.example/"
    db = await Database.connect(DSN)
    conn = db._conn
    now = datetime.now(UTC)
    old_item = now.replace(year=now.year - 6)
    try:
        run_id = await db.start_run()
        cases = {
            "ok": CheckRow(url=base + "ok", checked_at=now, last_item_date=now, title="ok"),
            "stale-item": CheckRow(
                url=base + "stale-item", checked_at=now, last_item_date=old_item
            ),
            "no-success": CheckRow(url=base + "no-success", checked_at=now, error="timeout"),
        }
        for row in cases.values():
            await db.insert_check(run_id, row)
        # an old success outside the window: parsed long ago, failing since
        await db.insert_check(
            run_id,
            CheckRow(
                url=base + "old-success",
                checked_at=now.replace(year=now.year - 1),
                last_item_date=now,
            ),
        )
        listed = {f.url for f in await db.listed_feeds(60, 5)}
        assert {u for u in listed if u.startswith(base)} == {base + "ok"}
        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url LIKE %s", (base + "%",))
    finally:
        await db.close()


async def test_delete_old_checks() -> None:
    """Checks older than the cutoff go; newer checks, feeds and inputs stay; runs left empty
    and older than the cutoff go too."""
    assert DSN is not None
    url = "https://indexer-cleanup.example/feed"
    db = await Database.connect(DSN)
    conn = db._conn
    now = datetime.now(UTC)
    old = now.replace(year=now.year - 1)
    try:
        await conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
        await db.upsert_inputs([InputRow(url, "src", "MIT")], {"MIT": 0})
        old_run = await db.start_run()
        await conn.execute("UPDATE runs SET started_at = %s WHERE id = %s", (old, old_run))
        await db.insert_check(old_run, CheckRow(url=url, checked_at=old, title="old"))
        new_run = await db.start_run()
        await db.insert_check(new_run, CheckRow(url=url, checked_at=now, title="new"))

        checks, runs = await db.delete_old_checks(8)
        assert checks >= 1 and runs >= 1
        cur = await conn.execute("SELECT run_id FROM health_checks WHERE url = %s", (url,))
        assert await cur.fetchall() == [(new_run,)]
        cur = await conn.execute("SELECT id FROM runs WHERE id = ANY(%s)", ([old_run, new_run],))
        assert await cur.fetchall() == [(new_run,)]
        cur = await conn.execute("SELECT title FROM feeds WHERE url = %s", (url,))
        assert await cur.fetchone() == ("new",)
        cur = await conn.execute("SELECT last_checked_at FROM inputs WHERE url = %s", (url,))
        assert await cur.fetchone() == (now,)

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (new_run,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (new_run,))
        await conn.execute("DELETE FROM feeds WHERE url = %s", (url,))
        await conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
    finally:
        await db.close()


async def test_delete_urls_from_every_table() -> None:
    """The listed URLs go from inputs, health_checks and feeds; others stay; unknown URLs are
    simply not in the result."""
    assert DSN is not None
    gone, kept = "https://indexer-undo.example/feed", "https://indexer-undo.example/keep"
    missing = "https://indexer-undo.example/never"
    db = await Database.connect(DSN)
    conn = db._conn
    try:
        for url in (gone, kept):
            await conn.execute("DELETE FROM health_checks WHERE url = %s", (url,))
            await conn.execute("DELETE FROM feeds WHERE url = %s", (url,))
            await conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
        await db.upsert_inputs([InputRow(gone, "s", "MIT"), InputRow(kept, "s", "MIT")], {"MIT": 0})
        run = await db.start_run()
        for url in (gone, kept):
            await db.insert_check(run, CheckRow(url=url, checked_at=datetime.now(UTC), title="t"))

        assert await db.delete_urls([gone, missing]) == {
            gone: {"health_checks": 1, "feeds": 1, "inputs": 1}
        }
        for table in ("inputs", "health_checks", "feeds"):
            cur = await conn.execute(
                f"SELECT url FROM {table} WHERE url = ANY(%s) ORDER BY url", ([gone, kept],)
            )
            assert await cur.fetchall() == [(kept,)]
        assert await db.delete_urls([gone]) == {}  # again: nothing left

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run,))
        await conn.execute("DELETE FROM feeds WHERE url = %s", (kept,))
        await conn.execute("DELETE FROM inputs WHERE url = %s", (kept,))
    finally:
        await db.close()


async def test_known_inputs() -> None:
    assert DSN is not None
    url = "https://indexer-test.example/known"
    db = await Database.connect(DSN)
    try:
        await db._conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
        assert await db.known_inputs([url, "https://indexer-test.example/x"]) == set()
        await db.upsert_inputs([InputRow(url, "src", "MIT")], {"MIT": 0})
        assert await db.known_inputs([url, "https://indexer-test.example/x"]) == {url}
        assert await db.known_inputs([]) == set()
        await db._conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
    finally:
        await db.close()


async def test_drop_all_tables_drops_only_our_tables() -> None:
    # In a scratch schema, so the migrated tables the other tests use are left alone.
    assert DSN is not None
    db = await Database.connect(DSN)
    try:
        await db._conn.execute("DROP SCHEMA IF EXISTS indexer_drop_test CASCADE")
        await db._conn.execute("CREATE SCHEMA indexer_drop_test")
        await db._conn.execute("SET search_path TO indexer_drop_test")
        for table in ("inputs", "runs", "databasechangelog", "someone_elses"):
            await db._conn.execute(f"CREATE TABLE {table} (id int)")
        assert await db.drop_all_tables() == ["databasechangelog", "inputs", "runs"]
        cur = await db._conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'indexer_drop_test'"
        )
        assert [r[0] for r in await cur.fetchall()] == ["someone_elses"]
        assert await db.drop_all_tables() == []  # nothing left to drop: no error
    finally:
        await db._conn.execute("DROP SCHEMA IF EXISTS indexer_drop_test CASCADE")
        await db.close()
