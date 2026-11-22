"""indexer cleanup: delete health checks older than a few months."""

import argparse
import sys

from indexer.cli.common import add_database_url, database

NAME = "cleanup"
HELP = "delete health checks older than --months"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--months", type=int, default=8, help="keep this many months of checks (default 8)"
    )
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    # The published lists need a success within --success-days (180); keep at least that much.
    if args.months < 7:
        print("error: --months must be at least 7 (export looks back 6 months)", file=sys.stderr)
        return 2
    async with database(args) as db:
        checks, runs = await db.delete_old_checks(args.months)
    print(f"deleted {checks} checks and {runs} empty runs older than {args.months} months")
    return 0
