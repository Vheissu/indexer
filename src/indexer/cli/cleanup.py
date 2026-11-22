"""indexer cleanup: keep only each feed's latest successful check and the recent failed ones;
clear stored recent posts after a few days."""

import argparse
import sys

from indexer.cli.common import add_database_url, database

NAME = "cleanup"
HELP = "delete old health checks; clear recent posts older than --posts-days"


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
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    # The published lists need a success within --success-days (180); keep at least that much.
    if args.months < 7:
        print("error: --months must be at least 7 (export looks back 6 months)", file=sys.stderr)
        return 2
    async with database(args) as db:
        checks, runs, posts = await db.delete_old_checks(args.months, args.posts_days)
    print(
        f"deleted {checks} checks and {runs} empty runs; cleared the recent posts of {posts}"
        f" checks older than {args.posts_days} days"
    )
    return 0
