"""indexer drop-tables: delete every table of this project (all data); needs --yes."""

import argparse
import sys

from indexer.cli.common import add_database_url, database

NAME = "drop-tables"
HELP = "delete every indexer table and all data (the next migration recreates them empty)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="confirm: really delete everything")
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "error: this deletes every indexer table and all their data; pass --yes",
            file=sys.stderr,
        )
        return 2
    async with database(args) as db:
        dropped = await db.drop_all_tables()
    print(f"dropped {len(dropped)} tables: {', '.join(dropped) or 'none existed'}")
    return 0
