"""indexer cleanup: keep only each feed's latest successful check and the recent failed ones;
clear stored recent posts after a few days; then report the database's size."""

import argparse
import sys
from pathlib import Path

from indexer.cli.common import add_database_url, database
from indexer.db.database import DatabaseSize

NAME = "cleanup"
HELP = "delete old health checks; clear recent posts older than --posts-days; report the size"
MB = 1_000_000


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--months", type=int, default=8, help="keep this many months of failed checks (default 8)"
    )
    parser.add_argument(
        "--posts-days",
        type=int,
        default=3,
        help="clear the recent posts of checks older than this (default 3)",
    )
    parser.add_argument(
        "--report-over-mb",
        type=int,
        default=300,
        help="warn when the database is bigger than this, in MB (default 300)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="when over --report-over-mb, also write the size report to this file (for an issue)",
    )
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    # The published lists need a success within --success-days (180); keep at least that much.
    if args.months < 7:
        print("error: --months must be at least 7 (export looks back 6 months)", file=sys.stderr)
        return 2
    async with database(args) as db:
        checks, runs, posts = await db.delete_old_checks(args.months, args.posts_days)
        size = await db.size()
    print(
        f"deleted {checks} checks and {runs} empty runs; cleared the recent posts of {posts}"
        f" checks older than {args.posts_days} days\n"
    )
    report = render_report(size, args.report_over_mb, args.posts_days)
    print(report, end="")
    if size.total > args.report_over_mb * MB:
        print(f"::warning title=Database size::{mb(size.total)}, over {args.report_over_mb} MB")
        if args.report is not None:
            args.report.write_text(report, encoding="utf-8")
    return 0


def mb(size: int) -> str:
    return f"{size / MB:.1f} MB"


def render_report(size: DatabaseSize, limit_mb: int, posts_days: int) -> str:
    """What takes the space, as a short Markdown report."""
    over = size.total > limit_mb * MB
    rest = size.total - sum(n for _, n in size.tables)
    lines = [
        f"The database takes {mb(size.total)}"
        + (f", over the {limit_mb} MB threshold." if over else f" (threshold: {limit_mb} MB).")
        + " Supabase's free tier allows 500 MB.",
        "",
        "| table | size (indexes included) |",
        "| --- | ---: |",
        *(f"| {name} | {mb(n)} |" for name, n in size.tables),
        f"| everything else (Postgres' and Liquibase's own tables) | {mb(rest)} |",
        "",
        f"Recent posts: {size.posts_checks:,} checks hold them, {mb(size.posts_bytes)} of"
        f" health_checks (cleared after {posts_days} days).",
    ]
    return "\n".join(lines) + "\n"
