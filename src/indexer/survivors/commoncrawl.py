"""Feeds from Common Crawl that are still active today (`indexer survivors`).

Common Crawl's WAT files hold metadata for every crawled page, including the `<link>` elements of
its HTML head. A page that advertises a feed has `<link rel="alternate" type="application/rss+xml"
href=...>`. survivors() takes a crawl from about five years ago, reads the first slice of one
random WAT file (a range request), collects every advertised feed URL (validated, excluded domains
dropped, session parameters and platform format variants folded, WordPress archive feeds
skipped), checks them all now and returns those that pass the agreed filters, for review.

Everything parsed is untrusted data; feed URLs found in a WAT file are only fetched by the guarded
checker. Downloads are not retried: if the slice fails, the run fails.
"""

import gzip
import io
import json
import logging
import random
import re
import time
import zlib
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from indexer.check.runner import CheckRow
from indexer.net.urls import drop_excluded, strip_session, valid_urls

# Progress of a survivors run: `indexer survivors` shows it (INFO) on stderr.
log = logging.getLogger("indexer.survivors")

INDEX_URL = "https://index.commoncrawl.org/collinfo.json"
DATA_URL = "https://data.commoncrawl.org/"
FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/rdf+xml"})
_CRAWL_ID = re.compile(r"CC-MAIN-\d{4}-\d{2}")
# WordPress archive feeds: a category, tag or author subset of the site's main feed.
_ARCHIVE_FEED = re.compile(r"/(?:category|tag|author)/.+/feed(?:/(?:rss2?|atom|rdf))?/?$", re.I)
# Alternate formats of one feed on common platforms, rewritten to a single URL before checking.
_WORDPRESS_FORMAT = re.compile(r"(/feed)/(?:atom|rdf|rss2?)/?$", re.I)  # /feed/atom/ -> /feed/
_WAT_PATH = re.compile(r"crawl-data/CC-MAIN-\d{4}-\d{2}/[A-Za-z0-9/._-]+\.warc\.wat\.gz")

type Fetch = Callable[[str], Awaitable[bytes]]


class _Readable(Protocol):
    def readline(self) -> bytes: ...
    def read(self, size: int = ...) -> bytes: ...


class CommonCrawlError(Exception):
    pass


@dataclass(slots=True)
class FileResult:
    path: str
    pages: int = 0
    pages_with_feed: int = 0
    feed_links: int = 0
    feeds: list[str] = field(default_factory=list)  # valid feed URLs, de-duplicated, in order


def crawl_near(collinfo: bytes, target: datetime) -> str:
    """The crawl whose crawl period ended closest to `target`."""
    try:
        crawls = json.loads(collinfo)
        dated = [
            (abs(datetime.fromisoformat(str(c["to"])).replace(tzinfo=UTC) - target), str(c["id"]))
            for c in crawls
            if isinstance(c, dict) and "to" in c and "id" in c
        ]
    except (ValueError, TypeError) as exc:
        raise CommonCrawlError(f"unexpected collinfo.json: {exc!r}") from exc
    if not dated:
        raise CommonCrawlError("collinfo.json lists no dated crawls")
    crawl = min(dated)[1]
    if not _CRAWL_ID.fullmatch(crawl):
        raise CommonCrawlError(f"unexpected crawl id {crawl!r}")
    return crawl


_CHUNK = 64 * 1024


def complete_prefix(data: bytes) -> bytes:
    """The leading complete gzip members of a range download (a WAT file is one gzip member
    per record), so a record cut off by the range is dropped, not misread."""
    # Fed in small chunks of a memoryview: passing the whole remainder for each member made this
    # quadratic (zlib copies everything after the member into unused_data), which took over an
    # hour for a 100 MB slice of ~300,000 members.
    view = memoryview(data)
    pos = 0
    while pos < len(data):
        member = zlib.decompressobj(wbits=31)
        fed = pos
        try:
            while not member.eof and fed < len(data):
                chunk = view[fed : fed + _CHUNK]
                fed += len(chunk)
                member.decompress(chunk)
        except zlib.error:
            break
        if not member.eof:
            break  # truncated by the range request
        pos = fed - len(member.unused_data)
    return data[:pos]


def wat_paths(paths_gz: bytes, crawl: str) -> list[str]:
    """The WAT file paths listed in a crawl's wat.paths.gz (only well-formed ones)."""
    try:
        text = gzip.decompress(paths_gz).decode("ascii", errors="replace")
    except (OSError, EOFError) as exc:
        raise CommonCrawlError(f"wat.paths.gz: {exc}") from exc
    paths = [
        p for p in text.split() if _WAT_PATH.fullmatch(p) and p.startswith(f"crawl-data/{crawl}/")
    ]
    if not paths:
        raise CommonCrawlError(f"no WAT paths for {crawl}")
    return paths


def _records(stream: _Readable) -> Iterator[tuple[dict[bytes, bytes], bytes]]:
    """WARC records from a decompressed stream: (headers, body)."""
    while True:
        line = stream.readline()
        if not line:
            return
        if not line.startswith(b"WARC/"):
            continue
        headers: dict[bytes, bytes] = {}
        while header := stream.readline().rstrip(b"\r\n"):
            key, _, value = header.partition(b":")
            headers[key.strip().lower()] = value.strip()
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return
        yield headers, stream.read(length)


def _page_feeds(envelope: dict[str, Any]) -> tuple[str, list[str]] | None:
    """(page URL, feed URLs) for an HTML page's metadata record; None for anything else."""
    page = envelope.get("WARC-Header-Metadata", {}).get("WARC-Target-URI")
    response = envelope.get("Payload-Metadata", {}).get("HTTP-Response-Metadata", {})
    if not isinstance(page, str) or "HTML-Metadata" not in response:
        return None
    links = response["HTML-Metadata"].get("Head", {}).get("Link", []) or []
    feeds: list[str] = []
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel", "")).lower().split()
        kind = str(link.get("type", "")).split(";")[0].strip().lower()
        href = link.get("url")
        if "alternate" in rel and kind in FEED_TYPES and isinstance(href, str) and href.strip():
            try:
                feeds.append(urljoin(page, href.strip()))
            except ValueError:
                continue
    return page, feeds


def scan_wat(data: bytes, path: str, exclude: frozenset[str] = frozenset()) -> FileResult:
    """Every feed URL advertised in one WAT file, validated like any input, minus excluded
    domains, de-duplicated by exact URL."""
    result = FileResult(path)
    candidates: dict[str, list[str]] = {}
    try:
        with gzip.open(io.BytesIO(data), "rb") as stream:
            for headers, body in _records(stream):
                if headers.get(b"warc-type") != b"metadata":
                    continue
                try:
                    envelope = json.loads(body)["Envelope"]
                except (ValueError, KeyError, TypeError):
                    continue
                found = _page_feeds(envelope) if isinstance(envelope, dict) else None
                if found is None:
                    continue
                result.pages += 1
                _page, feeds = found
                if feeds:
                    result.pages_with_feed += 1
                    result.feed_links += len(feeds)
                for feed in feeds:
                    candidates.setdefault(feed, [])
    except (OSError, EOFError) as exc:
        raise CommonCrawlError(f"{path}: {exc}") from exc
    valid, _invalid = valid_urls(candidates)
    valid, _excluded = drop_excluded(valid, exclude)
    result.feeds = [url for url, _host in valid]
    return result


# --- survivors: old-crawl feeds still active today, English, on https ------------------------

type Check = Callable[[Sequence[str]], Awaitable[Iterable[CheckRow]]]
# Which of these URLs are already proposed or in the inputs table.
type Known = Callable[[Sequence[str]], Awaitable[set[str]]]

CHECK_BATCH = 200  # feeds checked at a time, so a run can stop once it has enough


@dataclass(frozen=True, slots=True)
class Survivor:
    """A feed proposed for review: the URL to add, and what a reviewer needs to judge it."""

    url: str  # as served today (https, after redirects)
    wat_url: str  # the WAT file it was advertised in
    title: str | None
    language: str | None  # detected on the items: "en, fr"
    last_item_date: datetime
    item_links: tuple[str, ...] = ()  # each item's link as written in the feed (untrusted)


@dataclass(slots=True)
class SurvivorsResult:
    crawl: str
    file: FileResult  # the WAT slice that was read
    archive_feeds: int = 0  # WordPress category / tag / author feeds, skipped unchecked
    candidates: int = 0  # distinct feed URLs found in the slice
    known: int = 0  # found or served URLs already proposed or in inputs: skipped
    checked: int = 0  # feed URLs checked (fewer than candidates when the run stopped early)
    working: int = 0  # fetched and parsed today
    active: int = 0  # working, with an item in the last active_days days
    english: int = 0  # active and written in English (en among its languages)
    untitled: int = 0  # English, but no title
    keyword_excluded: int = 0  # English, but a [commoncrawl] exclude_keyword in the title
    rows: list[Survivor] = field(default_factory=list)  # pass every filter, new: the proposals


def is_active(row: CheckRow, now: datetime, active_days: int) -> bool:
    """Parsed today, and the newest item is from the last `active_days` days. A date more than a
    day in the future is not trusted."""
    date = row.last_item_date
    if row.error is not None or date is None:
        return False
    return now - timedelta(days=active_days) <= date <= now + timedelta(days=1)


def is_english(row: CheckRow) -> bool:
    """English is among the languages detected on the feed's items (`en`, see
    `indexer.language`). What the feed declares is not read."""
    return "en" in {t.strip() for t in (row.language or "").split(",")}


def has_keyword(row: CheckRow, keywords: frozenset[str]) -> bool:
    """The feed's title contains one of `keywords` (lower case) as a whole word: not inside
    another word, so "sex" matches "Sex Toys" and "sex-ed" but not "Essex"."""
    if not keywords:
        return False
    words = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    pattern = re.compile(rf"(?<![^\W_])(?:{words})(?![^\W_])")
    return pattern.search((row.title or "").lower()) is not None


def is_archive_feed(url: str) -> bool:
    """A WordPress category, tag or author feed (`/category/news/feed/`): a subset of the main
    feed, skipped before checking."""
    return bool(_ARCHIVE_FEED.search(urlsplit(url).path))


def canonical_format(url: str) -> str:
    """One URL for a feed that platforms offer in several formats, so the copies collapse into
    one candidate: WordPress `/feed/atom/`, `/feed/rdf/`, `/feed/rss/`, `/feed/rss2/` -> `/feed/`;
    Blogger `/feeds/...?alt=rss` -> the default (Atom) feed; Joomla `format=feed&type=atom` ->
    `type=rss`. The query is otherwise kept byte for byte."""
    parts = urlsplit(url)
    path = _WORDPRESS_FORMAT.sub(r"\1/", parts.path)
    segments = parts.query.split("&") if parts.query else []
    lowered = [seg.lower() for seg in segments]
    if path.lower().startswith("/feeds/"):
        segments = [seg for seg, low in zip(segments, lowered, strict=True) if low != "alt=rss"]
    elif "format=feed" in lowered:
        segments = [
            "type=rss" if low == "type=atom" else seg
            for seg, low in zip(segments, lowered, strict=True)
        ]
    query = "&".join(segments)
    if (path, query) == (parts.path, parts.query):
        return url
    return urlunsplit(parts._replace(path=path, query=query))


def served_url(row: CheckRow, exclude: frozenset[str] = frozenset()) -> str | None:
    """The URL the feed is served from today (after redirects), without session parameters, if
    it is https and valid, minus excluded domains (a redirect can lead anywhere); else None."""
    valid, _invalid = valid_urls([strip_session(row.final_url or row.url)])
    valid, _excluded = drop_excluded(valid, exclude)
    if not valid or not valid[0][0].startswith("https://"):
        return None
    return valid[0][0]


async def survivors(
    fetch: Fetch,
    fetch_slice: Fetch,
    check: Check,
    *,
    years_ago: int,
    active_days: int,
    max_feeds: int | None = None,
    known: Known | None = None,
    exclude: frozenset[str] = frozenset(),
    keywords: frozenset[str] = frozenset(),
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> SurvivorsResult:
    """Feeds advertised in an old crawl that are still active today.

    Takes the crawl that ended closest to `years_ago` years ago, the first slice (`fetch_slice`
    is a range request) of one random WAT file, every advertised feed URL in it (validated,
    minus excluded domains and WordPress category/tag/author feeds, session parameters removed,
    platform format variants folded into one URL, and those `known` reports dropped), and checks
    them now in random order, in batches, until `max_feeds` pass (all of them if None).
    Returned as new inputs, at most `max_feeds`: those that parsed, have an item from the last
    `active_days` days, are written in English (detected on their items), have a title and none
    of `keywords` in it, and are served over https today.
    Each is the URL after redirects (an http URL that redirects to https is kept as its https
    target), with the WAT file it was found in and the details a reviewer needs."""
    rng = rng or random.Random()
    now = now or datetime.now(UTC)
    started = time.monotonic()

    def progress(message: str, *args: object) -> None:
        log.info("[%4.0fs] " + message, time.monotonic() - started, *args)

    target = now - timedelta(days=round(365.25 * years_ago))  # no Feb 29 surprise
    progress("looking for the crawl that ended closest to %s", target.date())
    crawl = crawl_near(await fetch(INDEX_URL), target)
    paths = wat_paths(await fetch(f"{DATA_URL}crawl-data/{crawl}/wat.paths.gz"), crawl)
    path = rng.choice(paths)
    progress("crawl %s: %d WAT files; picked %s", crawl, len(paths), path)
    progress("downloading the start of %s", DATA_URL + path)
    data = await fetch_slice(DATA_URL + path)
    progress("downloaded %.1f MB", len(data) / 1e6)
    progress("decompressing and scanning the records")
    scanned = scan_wat(complete_prefix(data), path, exclude)
    progress(
        "scanned %d pages: %d advertise a feed (%d feed links, %d valid feed URLs)",
        scanned.pages,
        scanned.pages_with_feed,
        scanned.feed_links,
        len(scanned.feeds),
    )
    result = SurvivorsResult(crawl, scanned)
    found: set[str] = set()
    for url in scanned.feeds:
        if is_archive_feed(url):
            result.archive_feeds += 1
            continue
        found.add(canonical_format(strip_session(url)))
    result.candidates = len(found)
    order = sorted(found)
    rng.shuffle(order)  # stopping early then keeps a random subset
    if known is not None:
        already = await known(order)
        order = [url for url in order if url not in already]
        result.known += len(already)
    progress(
        "%d distinct feeds (%d category/tag/author feeds skipped, %d already known);"
        " looking for up to %s new feeds",
        len(found),
        result.archive_feeds,
        result.known,
        max_feeds if max_feeds is not None else "all",
    )
    rows: dict[str, Survivor] = {}  # served URL -> survivor (two found URLs can redirect to one)
    for start in range(0, len(order), CHECK_BATCH):
        batch = order[start : start + CHECK_BATCH]
        result.checked += len(batch)
        passed: dict[str, Survivor] = {}
        for row in await check(batch):
            if survivor := _passes(row, result, now, active_days, exclude, keywords):
                passed.setdefault(survivor.url, replace(survivor, wat_url=DATA_URL + path))
        if known is not None and passed:  # a redirect can lead to a URL we already have
            already = await known(list(passed))
            result.known += len(already)
            passed = {url: s for url, s in passed.items() if url not in already}
        for url, survivor in passed.items():
            rows.setdefault(url, survivor)
        progress("checked %d of %d: %d feeds pass", result.checked, len(order), len(rows))
        if max_feeds is not None and len(rows) >= max_feeds:
            progress("stopping: found %d", max_feeds)
            break
    result.rows = list(rows.values())[:max_feeds]
    progress("done: %d feeds pass every filter", len(result.rows))
    return result


def _passes(
    row: CheckRow,
    result: SurvivorsResult,
    now: datetime,
    active_days: int,
    exclude: frozenset[str],
    keywords: frozenset[str],
) -> Survivor | None:
    """The survivor for a checked row that passes every filter (counted in `result`), else
    None. Its wat_url is filled in by the caller."""
    result.working += row.error is None
    if not is_active(row, now, active_days):
        return None
    result.active += 1
    if not is_english(row):
        return None
    result.english += 1
    if not row.title:
        result.untitled += 1
        return None
    if has_keyword(row, keywords):
        result.keyword_excluded += 1
        return None
    https_url = served_url(row, exclude)
    if https_url is None or row.last_item_date is None:
        return None
    return Survivor(https_url, "", row.title, row.language, row.last_item_date, row.item_links)
