"""indexer embeddings: rebuild the embedding sets (data/embeddings/) with MiniLM."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from indexer.embeddings import EMBEDDINGS_DIR, EmbeddingsError
from indexer.topics import TOPICS_DIR, TopicsError, build_index

NAME = "embeddings"
HELP = "rebuild embedding sets (<name>.npy, <name>.json) from their sources with MiniLM"

# Every embedding set this command builds: name -> builder(directory) -> rows written.
EMBEDDING_SETS: dict[str, Callable[[Path], int]] = {
    "topics": lambda directory: build_index(TOPICS_DIR, embeddings=directory),
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "names",
        nargs="*",
        metavar="NAME",
        help=f"the sets to rebuild: {', '.join(EMBEDDING_SETS)} (default all)",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=EMBEDDINGS_DIR,
        help=f"where the sets are written (default {EMBEDDINGS_DIR})",
    )


async def run(args: argparse.Namespace) -> int:
    if unknown := [name for name in args.names if name not in EMBEDDING_SETS]:
        print(f"error: unknown embedding sets: {', '.join(unknown)}", file=sys.stderr)
        return 2
    for name in args.names or EMBEDDING_SETS:
        try:
            rows = EMBEDDING_SETS[name](args.directory)
        except (OSError, TopicsError, EmbeddingsError) as exc:
            print(f"error: cannot build the embedding set {name}: {exc}", file=sys.stderr)
            return 1
        print(f"{name}: {rows} rows embedded into {args.directory / name}.npy")
    return 0
