"""What several commands share: the database option and connection, a guarded fetch that returns
bodies, and loading the sources config and the models (printing why when that fails)."""

import argparse
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path

from indexer.db import DEFAULT_DATABASE_URL, Database
from indexer.embeddings import EmbeddingsError
from indexer.language import MODEL_ENV, LanguageModel
from indexer.net.fetch import FetchConfig, open_fetcher
from indexer.sources import SourceError, SourcesConfig, load_config
from indexer.topics import TopicModel, TopicsError

type BodyFetch = Callable[..., Awaitable[bytes]]  # fetch(url, headers=None) -> body


def add_database_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Postgres URL or libpq keyword string (default: $DATABASE_URL or the local"
        " compose database); the password may come from $PGPASSWORD",
    )


@contextlib.asynccontextmanager
async def database(args: argparse.Namespace) -> AsyncIterator[Database]:
    db = await Database.connect(args.database_url)
    try:
        yield db
    finally:
        await db.close()


@contextlib.asynccontextmanager
async def body_fetch(config: FetchConfig, error: type[Exception]) -> AsyncIterator[BodyFetch]:
    """A fetch(url) -> body through the guarded fetcher. A failure raises `error`."""
    async with open_fetcher(config) as fetcher:

        async def fetch(url: str, headers: Mapping[str, str] | None = None) -> bytes:
            result = await fetcher.fetch(url, headers)
            if result.error is None and result.body is not None:
                return result.body
            raise error(f"{url}: {result.error} {result.detail or ''}".strip())

        yield fetch


def sources_config(path: Path) -> SourcesConfig | None:
    """The sources config, or None after printing why it could not be read."""
    try:
        return load_config(path)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def language_model() -> LanguageModel | None:
    """The language model, or None after printing why it could not be loaded."""
    try:
        return LanguageModel.load()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: cannot load the language model ({MODEL_ENV}): {exc}", file=sys.stderr)
        return None


def topic_model() -> TopicModel | None:
    """The topic model, or None after printing why it could not be loaded."""
    try:
        return TopicModel.load()
    except (OSError, ValueError, RuntimeError, TopicsError, EmbeddingsError) as exc:
        print(
            f"error: cannot load the topic model, data/topics or data/embeddings: {exc}",
            file=sys.stderr,
        )
        return None
