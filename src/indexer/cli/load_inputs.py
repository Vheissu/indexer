"""indexer load-inputs: load the feed URLs from the lists in data/sources.toml into inputs."""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from indexer.cli.common import add_database_url, body_fetch, database, sources_config
from indexer.net.fetch import FetchConfig
from indexer.sources import SourceError, collect_inputs, download

NAME = "load-inputs"
HELP = "load every URL from the sources in a config into the inputs table"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("config", type=Path, help="sources config, e.g. data/sources.toml")
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    if (config := sources_config(args.config)) is None:
        return 2
    fetch_config = FetchConfig(deadline=120, max_body_bytes=32 * 1024 * 1024)  # large lists
    async with body_fetch(fetch_config, SourceError) as fetch:
        downloaded, failures = await download(config, fetch)
    if len(failures) == len(config.sources):
        print_failures(failures)
        return 1
    result = collect_inputs(config, downloaded)
    async with database(args) as db:
        inserted, reattributed = await db.upsert_inputs(result.rows, config.license_tiers)
    print(
        f"{len(result.rows)} unique URLs: {inserted} new, {reattributed} moved to a more"
        f" permissive source, {len(result.rows) - inserted - reattributed} unchanged"
    )
    for s in config.sources:
        if s.name not in failures:
            print(
                f"  {s.name:<28} {s.license:<9} {len(s.urls) + len(s.files):>3} file(s)"
                f"  credited {result.attributed[s.name]:>6} of {result.found[s.name]:>6} found"
                + (f"  ({result.invalid[s.name]} invalid)" if result.invalid[s.name] else "")
                + (f"  ({result.excluded[s.name]} excluded)" if result.excluded[s.name] else "")
            )
    print_failures(failures)
    return sources_exit_code(failures, len(config.sources))


def print_failures(failures: Mapping[str, str]) -> None:
    """Report skipped sources. In GitHub Actions also as warning annotations,
    which show on the run's summary page without failing the run."""
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for name, reason in failures.items():
        print(f"  {name:<28} SKIPPED: {reason}", file=sys.stderr)
        if in_actions:
            text = f"{name}: {reason}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::warning title=Source skipped::{text}")


def sources_exit_code(failures: Mapping[str, str], total: int) -> int:
    """A skipped source is a warning, not a failure: upstream lists break now and then, and the
    others still load. Fail only when no source could be read at all."""
    return 1 if len(failures) >= total else 0
