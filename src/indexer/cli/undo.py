"""indexer undo: delete the URLs listed in a file from every table."""

import argparse
import sys
from pathlib import Path

from indexer.cli.common import add_database_url, database
from indexer.db import UNDO_TABLES
from indexer.errors import CheckError
from indexer.net.validate import read_url_file, validate_url

NAME = "undo"
HELP = (
    "delete the URLs listed in FILE from inputs, health_checks and feeds, and report which"
    " were deleted and which were not found"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, help="one URL per line, # comments (e.g. data/undo.txt)")
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    try:
        urls = read_url_file(args.file)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    forms = {url: _forms(url) for url in urls}
    found: dict[str, dict[str, int]] = {}
    if urls:
        async with database(args) as db:
            found = await db.delete_urls([f for fs in forms.values() for f in fs])
    deleted, missing = [], []
    for url, fs in forms.items():
        counts = dict.fromkeys(UNDO_TABLES, 0)
        for f in dict.fromkeys(fs):
            for table, n in found.get(f, {}).items():
                counts[table] += n
        if any(counts.values()):
            rows = ", ".join(f"{table} {n}" for table, n in counts.items())
            deleted.append(f"  {url}  ({rows})")
        else:
            missing.append(f"  {url}")
    print(f"{len(urls)} URLs in {args.file}: {len(deleted)} deleted, {len(missing)} not found")
    if deleted:
        print("deleted (rows per table):", *deleted, sep="\n")
    if missing:
        print("not found (in no table):", *missing, sep="\n")
    return 0


def _forms(url: str) -> list[str]:
    """The URL as written, and as inputs stores it (canonical) when that differs."""
    try:
        canonical = str(validate_url(url))
    except CheckError:
        return [url]
    return [url] if canonical == url else [url, canonical]
