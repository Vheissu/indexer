"""indexer run: check a URL file, or a random batch of inputs, and record one row per URL."""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from indexer.check.runner import CheckRow, run_checks
from indexer.cli.common import add_database_url, database, language_model, topic_model
from indexer.net.fetch import FetchConfig, open_fetcher
from indexer.net.validate import read_url_file

NAME = "run"
HELP = "check every URL in FILE, or a random batch from the inputs table"

log = logging.getLogger("indexer")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "file", type=Path, nargs="?", help="text file, one URL per line, # for comments"
    )
    source.add_argument(
        "--from-inputs",
        action="store_true",
        help="check a random batch of inputs not checked in the last --stale-days days",
    )
    parser.add_argument(
        "--batch", type=int, default=500, help="batch size with --from-inputs (default 500)"
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=14,
        help="with --from-inputs, recheck URLs last checked more than this many days ago"
        " (default 14)",
    )
    parser.add_argument("--concurrency", type=int, default=20, help="fetches at once (default 20)")
    parser.add_argument("--per-host", type=int, default=2, help="fetches per host (default 2)")
    parser.add_argument("--deadline", type=float, default=7.0, help="seconds per fetch (default 7)")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each failed URL with the reason"
    )
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    if args.from_inputs and (args.batch < 1 or args.stale_days < 0):
        print("error: --batch must be positive and --stale-days not negative", file=sys.stderr)
        return 2
    if (languages := language_model()) is None or (model := topic_model()) is None:
        return 2
    config = FetchConfig(
        deadline=args.deadline,
        concurrency=args.concurrency,
        per_host=args.per_host,
    )
    failed_writes = 0
    checked = 0
    async with database(args) as db:
        if args.from_inputs:
            urls = await db.select_batch(args.batch, args.stale_days)
            if not urls:
                print(
                    f"nothing to check: every input was checked in the last {args.stale_days} days"
                )
                return 0
        else:
            urls = read_url_file(args.file)
        run_id = await db.start_run()
        log.info("run %d: checking %d URLs", run_id, len(urls))

        async def record(row: CheckRow) -> None:
            nonlocal failed_writes, checked
            if args.verbose:
                log_failure(row)
            try:
                await db.insert_check(run_id, row)
            except Exception:
                failed_writes += 1
                log.exception("could not store the result for %s", row.url)
            checked += 1
            if checked % 100 == 0:
                print(f"{checked} feeds checked", flush=True)

        async with open_fetcher(config) as fetcher:
            rows = await run_checks(urls, fetcher, on_row=record, languages=languages, topics=model)
        await db.finish_run(run_id)

    counts = Counter(row.error or "ok" for row in rows)
    print(f"run {run_id}: {len(rows)} URLs")
    for outcome, n in counts.most_common():
        print(f"  {outcome:<22} {n}")
    if failed_writes:
        print(f"  {failed_writes} results could not be stored", file=sys.stderr)
        return 1
    return 0


def log_failure(row: CheckRow) -> None:
    """With -v: one line (plus details) per failed URL. Successes only count in the summary."""
    if row.error is None:
        return
    log.info("%-20s %6s ms  %s", row.error, row.fetch_time_ms, row.url)
    if row.detail:
        log.info("%28s%s", "", row.detail)
