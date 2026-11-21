"""indexer: secure RSS/Atom feed health checks.

Commands, in pipeline order:
  load-inputs  load the feed URLs from the lists in data/sources.toml into the inputs table
  survivors    propose feeds from a ~5-year-old Common Crawl crawl still active today
  run          check a URL file, or a random batch of inputs, and record one row per URL
  export       write the published lists (output/topic/<topic>/...) and website (output/docs/)
  cleanup      delete health checks older than a few months
  drop-tables  delete every table of this project (all data); needs --yes
  topics       the topics of some text (no feed, no database): to try the topic model
  languages    the languages of some text (no feed, no database): to try the language model
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indexer.check.runner import CheckRow, run_checks
from indexer.db import DEFAULT_DATABASE_URL, UNDO_TABLES, Database
from indexer.errors import CheckError
from indexer.export import DATA_URL, SITE_URL, ExportError, with_synonyms, write_output
from indexer.language import MODEL_ENV, LanguageModel
from indexer.net.fetch import FetchConfig, open_fetcher
from indexer.net.validate import read_url_file, validate_url
from indexer.sources import (
    SourceError,
    SourcesConfig,
    collect_inputs,
    download,
    load_config,
)
from indexer.survivors import proposals
from indexer.survivors import run as survivors_run
from indexer.survivors.wat import CommonCrawlError
from indexer.topics import (
    TOPICS_DIR,
    Post,
    TopicModel,
    TopicsError,
    build_index,
    read_synonyms,
)

log = logging.getLogger("indexer")

type BodyFetch = Callable[..., Awaitable[bytes]]  # fetch(url, headers=None) -> body
type Handler = Callable[[argparse.Namespace], Coroutine[Any, Any, int]]


# --- shared helpers ------------------------------------------------------------------------------


def _add_database_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Postgres URL or libpq keyword string (default: $DATABASE_URL or the local"
        " compose database); the password may come from $PGPASSWORD",
    )


@contextlib.asynccontextmanager
async def _database(args: argparse.Namespace) -> AsyncIterator[Database]:
    db = await Database.connect(args.database_url)
    try:
        yield db
    finally:
        await db.close()


@contextlib.asynccontextmanager
async def _body_fetch(
    config: FetchConfig,
    error: type[Exception],
) -> AsyncIterator[BodyFetch]:
    """A fetch(url) -> body through the guarded fetcher. A failure raises `error`."""
    async with open_fetcher(config) as fetcher:

        async def fetch(url: str, headers: Mapping[str, str] | None = None) -> bytes:
            result = await fetcher.fetch(url, headers)
            if result.error is None and result.body is not None:
                return result.body
            raise error(f"{url}: {result.error} {result.detail or ''}".strip())

        yield fetch


def _load_config(path: Path) -> SourcesConfig | None:
    try:
        return load_config(path)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _print_failures(failures: Mapping[str, str]) -> None:
    """Report skipped sources. In GitHub Actions also as warning annotations,
    which show on the run's summary page without failing the run."""
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for name, reason in failures.items():
        print(f"  {name:<28} SKIPPED: {reason}", file=sys.stderr)
        if in_actions:
            text = f"{name}: {reason}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::warning title=Source skipped::{text}")


def _sources_exit_code(failures: Mapping[str, str], total: int) -> int:
    """A skipped source is a warning, not a failure: upstream lists break now and then, and the
    others still load. Fail only when no source could be read at all."""
    return 1 if len(failures) >= total else 0


def _log_failure(row: CheckRow) -> None:
    """With -v: one line (plus details) per failed URL. Successes only count in the summary."""
    if row.error is None:
        return
    log.info("%-20s %6s ms  %s", row.error, row.fetch_time_ms, row.url)
    if row.detail:
        log.info("%28s%s", "", row.detail)


def _language_model() -> LanguageModel | None:
    """The language model, or None after printing why it could not be loaded."""
    try:
        return LanguageModel.load()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: cannot load the language model ({MODEL_ENV}): {exc}", file=sys.stderr)
        return None


def _topic_model() -> TopicModel | None:
    """The topic model, or None after printing why it could not be loaded."""
    try:
        return TopicModel.load()
    except (OSError, ValueError, RuntimeError, TopicsError) as exc:
        print(f"error: cannot load the topic model or data/topics: {exc}", file=sys.stderr)
        return None


# --- commands ------------------------------------------------------------------------------------


async def _load_inputs(args: argparse.Namespace) -> int:
    if (config := _load_config(args.config)) is None:
        return 2
    fetch_config = FetchConfig(deadline=120, max_body_bytes=32 * 1024 * 1024)  # large lists
    async with _body_fetch(fetch_config, SourceError) as fetch:
        downloaded, failures = await download(config, fetch)
    if len(failures) == len(config.sources):
        _print_failures(failures)
        return 1
    result = collect_inputs(config, downloaded)
    async with _database(args) as db:
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
    _print_failures(failures)
    return _sources_exit_code(failures, len(config.sources))


async def _survivors(args: argparse.Namespace) -> int:
    if min(args.slice_mb, args.years_ago, args.active_days, args.max_feeds) < 1:
        print(
            "error: --slice-mb, --years-ago, --active-days and --max-feeds must be positive",
            file=sys.stderr,
        )
        return 2
    if (config := _load_config(args.config)) is None:
        return 2
    if (languages := _language_model()) is None:
        return 2
    started = datetime.now(UTC)
    slice_bytes = args.slice_mb * 1024 * 1024
    download = FetchConfig(deadline=900, max_body_bytes=slice_bytes + 1024 * 1024)
    in_file = proposals.known_urls(args.dir)
    try:
        async with (
            _database(args) as db,
            _body_fetch(download, CommonCrawlError) as fetch_once,
            open_fetcher(FetchConfig()) as checker,
        ):

            async def fetch_slice(url: str) -> bytes:
                return await fetch_once(url, {"Range": f"bytes=0-{slice_bytes - 1}"})

            async def check(urls: Sequence[str]) -> list[CheckRow]:
                return await run_checks(urls, checker, languages=languages)

            async def known(urls: Sequence[str]) -> set[str]:
                return {u for u in urls if u in in_file} | await db.known_inputs(list(urls))

            survivors_run.log.setLevel(logging.INFO)  # show the run's progress
            result = await survivors_run.survivors(
                fetch_once,
                fetch_slice,
                check,
                years_ago=args.years_ago,
                active_days=args.active_days,
                max_feeds=args.max_feeds,
                known=known,
                exclude=config.exclude_domains,
                keywords=config.commoncrawl_exclude_keywords,
            )
    except CommonCrawlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    new = result.rows
    stopped = (
        " (stopped: enough feeds)" if result.checked < result.candidates - result.known else ""
    )
    summary = (
        f"{args.slice_mb} MB of {result.scan.path.rsplit('/', 1)[-1]}:"
        f" {result.candidates} feed URLs ({result.known} already known),"
        f" {result.checked} checked{stopped},"
        f" {result.working} working today, {result.active} with an item in the last"
        f" {args.active_days} days, {result.english} in English"
        f" ({result.rejected['untitled']} without a title,"
        f" {result.rejected['keyword']} with an excluded keyword,"
        f" {result.rejected['no_subtitle']} without a description,"
        f" {result.rejected['wiki']} wiki edits,"
        f" {result.rejected['tracking']} with tracking links,"
        f" {result.rejected['seo_footer']} with an SEO footer,"
        f" {result.rejected['sample_post']} with WordPress's sample post);"
        f" acceptance {proposals.acceptance(len(new), result.checked)}"
    )
    path = args.dir / proposals.file_name(started)
    print(f"{result.crawl}: {summary}: {len(new)} proposed in {path}")
    for survivor in new:
        print(proposals.log_line(survivor))
    print(
        f"Acceptance: {proposals.acceptance(len(new), result.checked)}"
        " of the checked feed URLs proposed"
    )
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proposals.render_entries(new, result.crawl, started.date()), "utf-8")
        path.with_suffix(".opml").write_text(
            proposals.render_opml(new, result.crawl, datetime.now(UTC)), "utf-8"
        )
        if args.report:
            args.report.write_text(
                proposals.render_report(new, result.crawl, summary, datetime.now(UTC)),
                encoding="utf-8",
            )
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.from_inputs and (args.batch < 1 or args.stale_days < 0):
        print("error: --batch must be positive and --stale-days not negative", file=sys.stderr)
        return 2
    if (languages := _language_model()) is None or (model := _topic_model()) is None:
        return 2
    config = FetchConfig(
        deadline=args.deadline,
        concurrency=args.concurrency,
        per_host=args.per_host,
    )
    failed_writes = 0
    checked = 0
    async with _database(args) as db:
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
                _log_failure(row)
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


async def _export(args: argparse.Namespace) -> int:
    if (args.top_topics is not None and args.top_topics < 1) or min(
        args.success_days, args.item_years
    ) < 1:
        print(
            "error: --top-topics, --success-days and --item-years must be positive",
            file=sys.stderr,
        )
        return 2
    try:
        synonyms = read_synonyms(args.synonyms)
    except OSError as exc:
        print(f"error: cannot read the synonyms ({args.synonyms}): {exc}", file=sys.stderr)
        return 1
    async with _database(args) as db:
        feeds = with_synonyms(await db.listed_feeds(args.success_days, args.item_years), synonyms)
    try:
        topics = write_output(
            args.output_dir,
            feeds,
            top=args.top_topics,
            generated=datetime.now(UTC),
            success_days=args.success_days,
            item_years=args.item_years,
            spec=args.spec,
            site_url=args.site_url,
            data_url=args.data_url,
        )
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    in_topics = len({f.url for t in topics for f in t.feeds})
    print(
        f"{len(feeds)} feeds qualify (all in all/); wrote {len(topics)} topics covering"
        f" {in_topics} feeds to {args.output_dir}/"
    )
    return 0


async def _cleanup(args: argparse.Namespace) -> int:
    # The published lists need a success within --success-days (180); keep at least that much.
    if args.months < 7:
        print("error: --months must be at least 7 (export looks back 6 months)", file=sys.stderr)
        return 2
    async with _database(args) as db:
        checks, runs = await db.delete_old_checks(args.months)
    print(f"deleted {checks} checks and {runs} empty runs older than {args.months} months")
    return 0


def _undo_forms(url: str) -> list[str]:
    """The URL as written, and as inputs stores it (canonical) when that differs."""
    try:
        canonical = str(validate_url(url))
    except CheckError:
        return [url]
    return [url] if canonical == url else [url, canonical]


async def _undo(args: argparse.Namespace) -> int:
    try:
        urls = read_url_file(args.file)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    forms = {url: _undo_forms(url) for url in urls}
    found: dict[str, dict[str, int]] = {}
    if urls:
        async with _database(args) as db:
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


async def _drop_tables(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "error: this deletes every indexer table and all their data; pass --yes",
            file=sys.stderr,
        )
        return 2
    async with _database(args) as db:
        dropped = await db.drop_all_tables()
    print(f"dropped {len(dropped)} tables: {', '.join(dropped) or 'none existed'}")
    return 0


# --- argument parsing ----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indexer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, handler: Handler, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(handler=handler)
        return p

    li = command(
        "load-inputs",
        _load_inputs,
        "load every URL from the sources in a config into the inputs table",
    )
    li.add_argument("config", type=Path, help="sources config, e.g. data/sources.toml")
    _add_database_url(li)

    sv = command(
        "survivors",
        _survivors,
        "propose feeds from a ~5-year-old crawl still active today (English, https)"
        " by appending them to --file for review",
    )
    sv.add_argument(
        "--config",
        type=Path,
        default=Path("data/sources.toml"),
        help="for [exclude] domains and the [licenses] ranking (default data/sources.toml)",
    )
    sv.add_argument(
        "--years-ago", type=int, default=5, help="age of the crawl to sample (default 5)"
    )
    sv.add_argument(
        "--dir",
        type=Path,
        default=Path("data/survivors"),
        help="write proposals to a new <yyyymm>/<dd-HHMMSS>.txt here (UTC), and the same feeds"
        " as OPML in a .opml file beside it; URLs already in"
        " its files or in inputs are skipped (default data/survivors)",
    )
    sv.add_argument(
        "--report", type=Path, help="also write the proposals as Markdown (for the pull request)"
    )
    sv.add_argument(
        "--slice-mb",
        type=int,
        default=25,
        help="download the first N MB of one random WAT file (default 25)",
    )
    sv.add_argument(
        "--active-days",
        type=int,
        default=243,
        help="keep feeds with an item in the last N days (default 243, about 8 months)",
    )
    sv.add_argument(
        "--max-feeds",
        type=int,
        default=20,
        help="stop checking once this many new feeds pass (default 20)",
    )
    _add_database_url(sv)

    run = command("run", _run, "check every URL in FILE, or a random batch from the inputs table")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "file", type=Path, nargs="?", help="text file, one URL per line, # for comments"
    )
    source.add_argument(
        "--from-inputs",
        action="store_true",
        help="check a random batch of inputs not checked in the last --stale-days days",
    )
    run.add_argument(
        "--batch", type=int, default=500, help="batch size with --from-inputs (default 500)"
    )
    run.add_argument(
        "--stale-days",
        type=int,
        default=14,
        help="with --from-inputs, recheck URLs last checked more than this many days ago"
        " (default 14)",
    )
    run.add_argument("--concurrency", type=int, default=20, help="fetches at once (default 20)")
    run.add_argument("--per-host", type=int, default=2, help="fetches per host (default 2)")
    run.add_argument("--deadline", type=float, default=7.0, help="seconds per fetch (default 7)")
    run.add_argument(
        "-v", "--verbose", action="store_true", help="print each failed URL with the reason"
    )
    _add_database_url(run)

    ex = command(
        "export",
        _export,
        "write the data files (output/topic/<topic>/...) and the website (output/docs/)",
    )
    ex.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="cleared first (default output)",
    )
    ex.add_argument(
        "--spec",
        type=Path,
        default=Path("SPEC.md"),
        help="copied into the output as SPEC.md (default SPEC.md)",
    )
    ex.add_argument(
        "--synonyms",
        type=Path,
        default=TOPICS_DIR / "synonyms.tsv",
        help="topic names mapped to their canonical name before publishing"
        f" (default {TOPICS_DIR / 'synonyms.tsv'})",
    )
    ex.add_argument(
        "--site-url",
        default=SITE_URL,
        help=f"where output/docs/ is served, ending with / (default {SITE_URL})",
    )
    ex.add_argument(
        "--data-url",
        default=DATA_URL,
        help=f"where the output's data files are served, ending with / (default {DATA_URL})",
    )
    ex.add_argument(
        "--top-topics", type=int, help="publish only this many topics, the largest (default: all)"
    )
    ex.add_argument(
        "--success-days",
        type=int,
        default=180,
        help="list feeds checked successfully within this many days (default 180, about 6 months)",
    )
    ex.add_argument(
        "--item-years",
        type=int,
        default=1,
        help="list feeds whose newest item is younger than this (default 1)",
    )
    _add_database_url(ex)

    cl = command("cleanup", _cleanup, "delete health checks older than --months")
    cl.add_argument(
        "--months", type=int, default=8, help="keep this many months of checks (default 8)"
    )
    _add_database_url(cl)

    ud = command(
        "undo",
        _undo,
        "delete the URLs listed in FILE from inputs, health_checks and feeds, and report which"
        " were deleted and which were not found",
    )
    ud.add_argument("file", type=Path, help="one URL per line, # comments (e.g. data/undo.txt)")
    _add_database_url(ud)

    dt = command(
        "drop-tables",
        _drop_tables,
        "delete every indexer table and all data (the next migration recreates them empty)",
    )
    dt.add_argument("--yes", action="store_true", help="confirm: really delete everything")
    _add_database_url(dt)

    tp = command(
        "topics",
        _topics,
        "print the topics of some posts (each TEXT is one post: its title and opening), one per"
        " line, most similar first; nothing is fetched or stored",
    )
    tp.add_argument("text", nargs="+", metavar="TEXT", help="a post's text, in English")
    tp.add_argument("--title", help="the feed's title")
    tp.add_argument("--subtitle", help="the feed's subtitle (counts only if it says something)")
    tp.add_argument(
        "--tags",
        action="append",
        default=[],
        metavar="A,B",
        help="one post's tags, comma separated; repeat in the order of the TEXTs",
    )

    em = command(
        "embeddings",
        _embeddings,
        "rebuild the topic matrix (topics.npy, topics.json) from topics.txt with MiniLM",
    )
    em.add_argument(
        "--directory",
        type=Path,
        default=TOPICS_DIR,
        help=f"holds topics.txt; the files are written there (default {TOPICS_DIR})",
    )

    lg = command(
        "languages",
        _languages,
        "print the language of each TEXT (one post each: its title and opening), then the feed's"
        " labels; nothing is fetched or stored",
    )
    lg.add_argument("text", nargs="+", metavar="TEXT", help="a post's text")

    return parser


async def _topics(args: argparse.Namespace) -> int:
    if (model := _topic_model()) is None:
        return 2
    tags = [[t.strip() for t in group.split(",") if t.strip()] for group in args.tags]
    posts = [Post(text, tags[i] if i < len(tags) else ()) for i, text in enumerate(args.text)]
    for topic in model.topics(posts, args.title, args.subtitle):
        print(topic)
    return 0


async def _embeddings(args: argparse.Namespace) -> int:
    try:
        rows = build_index(args.directory)
    except (OSError, TopicsError) as exc:
        print(f"error: cannot build the topic matrix: {exc}", file=sys.stderr)
        return 1
    print(f"{rows} topic names embedded into {args.directory / 'topics.npy'}")
    return 0


async def _languages(args: argparse.Namespace) -> int:
    if (model := _language_model()) is None:
        return 2
    for text, code in zip(args.text, model.post_languages(args.text), strict=True):
        shown = "(too short)" if code is None else code or "(unknown)"
        print(f"{shown:12} {' '.join(text.split())[:70]}")
    print(f"feed: {', '.join(model.languages(args.text)) or '(none)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(message)s",
        stream=sys.stderr,
    )
    handler: Handler = args.handler
    return asyncio.run(handler(args))
