"""`indexer survivors`: feeds advertised in a ~5-year-old Common Crawl crawl that are still
active today, proposed for review.

    1. pick the crawl closest to `years_ago` and one random WAT file of it      wat.py
    2. download the file's first slice and list the feeds its pages advertise   wat.py
    3. skip archive and comment feeds, fold variants, drop URLs we already know filters.py
    4. check the rest in random order, a batch at a time, keep those that pass  filters.py
    5. stop once `max_feeds` pass

The fetching, checking and "do we know this URL" functions are passed in (the CLI gives the real
ones, tests give fakes).
"""

import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from indexer.check.runner import CheckRow
from indexer.net.urls import strip_session
from indexer.survivors.filters import (
    Reason,
    Rules,
    canonical_format,
    is_archive_feed,
    is_comment_feed,
    rejection,
    served_url,
)
from indexer.survivors.wat import DATA_URL, Fetch, WatScan, complete_prefix, pick_wat_file, scan_wat

# Progress of a run: `indexer survivors` shows it (INFO) on stderr.
log = logging.getLogger("indexer.survivors")

_CHECK_BATCH = 200  # feeds checked at a time, so a run can stop once it has enough

type _Check = Callable[[Sequence[str]], Awaitable[Iterable[CheckRow]]]
# Which of these URLs are already proposed or in the inputs table.
type _Known = Callable[[Sequence[str]], Awaitable[set[str]]]


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
    scan: WatScan  # the WAT slice that was read
    archive_feeds: int = 0  # WordPress category / tag / author feeds, skipped unchecked
    comment_feeds: int = 0  # comment feeds, skipped unchecked
    candidates: int = 0  # distinct feed URLs left to consider
    known: int = 0  # already proposed or in inputs (before or after redirects): skipped
    checked: int = 0  # fewer than candidates when the run stopped early
    rejected: Counter[Reason] = field(default_factory=Counter)  # checked, failed a rule
    rows: list[Survivor] = field(default_factory=list)  # passed every rule: the proposals

    # The funnel, for the summary: each count is the previous one minus a rejection reason.
    @property
    def working(self) -> int:
        return self.checked - self.rejected["failed"]

    @property
    def active(self) -> int:
        return self.working - self.rejected["inactive"]

    @property
    def english(self) -> int:
        return self.active - self.rejected["not_english"]


async def survivors(
    fetch: Fetch,
    fetch_slice: Fetch,
    check: _Check,
    *,
    years_ago: int,
    active_days: int,
    max_feeds: int | None = None,
    known: _Known | None = None,
    exclude: frozenset[str] = frozenset(),
    keywords: frozenset[str] = frozenset(),
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> SurvivorsResult:
    """Runs steps 1-5 above. `fetch_slice` is the range request for the WAT file; `known` (if
    given) drops URLs we already have; `exclude` holds excluded domains, `keywords` excluded
    title words. At most `max_feeds` survivors are returned (all of them if None)."""
    rng = rng or random.Random()
    now = now or datetime.now(UTC)
    known = known or _nothing_known
    rules = Rules(now, active_days, exclude, keywords)

    # 1-2. One WAT slice and the feeds it advertises.
    target = now - timedelta(days=round(365.25 * years_ago))  # no Feb 29 surprise
    crawl, path = await pick_wat_file(fetch, target, rng)
    wat_url = DATA_URL + path
    log.info(
        "crawl %s (ended closest to %s): downloading the start of %s", crawl, target.date(), wat_url
    )
    data = await fetch_slice(wat_url)
    scan = scan_wat(complete_prefix(data), path, exclude)
    log.info(
        "%.1f MB: %d pages, %d advertise a feed, %d valid feed URLs",
        len(data) / 1e6,
        scan.pages,
        scan.pages_with_feed,
        len(scan.feeds),
    )
    result = SurvivorsResult(crawl, scan)

    # 3. The URLs worth checking.
    to_check = await _candidates(scan.feeds, result, known, rng)
    log.info(
        "%d candidates (%d archive and %d comment feeds skipped, %d already known), %d to check",
        result.candidates,
        result.archive_feeds,
        result.comment_feeds,
        result.known,
        len(to_check),
    )

    # 4-5. Check a batch at a time until enough pass.
    passed: dict[str, Survivor] = {}  # by served URL: two found URLs can redirect to one
    for start in range(0, len(to_check), _CHECK_BATCH):
        batch = to_check[start : start + _CHECK_BATCH]
        result.checked += len(batch)
        new = [s for row in await check(batch) if (s := _judge(row, rules, wat_url, result))]
        for survivor in await _unknown(new, known, result):
            passed.setdefault(survivor.url, survivor)
        log.info("checked %d of %d: %d pass", result.checked, len(to_check), len(passed))
        if max_feeds is not None and len(passed) >= max_feeds:
            break
    result.rows = list(passed.values())[:max_feeds]
    return result


async def _candidates(
    feeds: Iterable[str], result: SurvivorsResult, known: _Known, rng: random.Random
) -> list[str]:
    """The advertised feeds worth checking, in random order (so stopping early keeps a random
    subset): archive and comment feeds skipped, session parameters stripped, format variants
    folded into one URL, known URLs dropped. Counts archive_feeds, comment_feeds, candidates
    and known."""
    urls: set[str] = set()
    for url in feeds:
        if is_archive_feed(url):
            result.archive_feeds += 1
        elif is_comment_feed(url):
            result.comment_feeds += 1
        else:
            urls.add(canonical_format(strip_session(url)))
    result.candidates = len(urls)
    order = sorted(urls)  # sorted first, so a seeded rng gives the same order every time
    rng.shuffle(order)
    already = await known(order)
    result.known += len(already)
    return [url for url in order if url not in already]


def _judge(row: CheckRow, rules: Rules, wat_url: str, result: SurvivorsResult) -> Survivor | None:
    """The survivor for a checked feed that passes every rule; else None, its reason counted."""
    if reason := rejection(row, rules):
        result.rejected[reason] += 1
        return None
    url = served_url(row, rules.exclude)
    assert url is not None and row.last_item_date is not None  # rejection() made sure
    return Survivor(url, wat_url, row.title, row.language, row.last_item_date, row.item_links)


async def _unknown(
    survivors: list[Survivor], known: _Known, result: SurvivorsResult
) -> list[Survivor]:
    """Drops survivors whose served URL we already have (a redirect can lead to one)."""
    if not survivors:
        return []
    already = await known([s.url for s in survivors])
    result.known += len(already)
    return [s for s in survivors if s.url not in already]


async def _nothing_known(urls: Sequence[str]) -> set[str]:
    return set()
