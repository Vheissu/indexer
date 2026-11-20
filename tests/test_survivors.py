"""Survivors: reading WAT files, the rules, the pipeline (synthetic files, no network)."""

import gzip
import json
import os
import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from indexer.check.runner import CheckRow
from indexer.survivors.filters import (
    _has_keyword,
    _is_active,
    _is_english,
    canonical_format,
    is_archive_feed,
    served_url,
)
from indexer.survivors.run import _CHECK_BATCH, survivors
from indexer.survivors.wat import (
    _INDEX_URL,
    DATA_URL,
    CommonCrawlError,
    Fetch,
    _crawl_near,
    _wat_paths,
    complete_prefix,
    scan_wat,
)

CRAWL = "CC-MAIN-2026-43"
PATH = f"crawl-data/{CRAWL}/segments/1/wat/CC-MAIN-1-00001.warc.wat.gz"


def record(body: bytes, warc_type: str = "metadata") -> bytes:
    head = f"WARC/1.0\r\nWARC-Type: {warc_type}\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode() + body + b"\r\n\r\n"


def page(url: str, links: list[dict[str, str]] | None, html: bool = True) -> bytes:
    response: dict[str, object] = {}
    if html:
        response["HTML-Metadata"] = {"Head": {"Link": links or []}}
    envelope = {
        "WARC-Header-Metadata": {"WARC-Target-URI": url},
        "Payload-Metadata": {"HTTP-Response-Metadata": response},
    }
    return record(json.dumps({"Envelope": envelope}).encode())


def feed_link(href: str, kind: str = "application/rss+xml") -> dict[str, str]:
    return {"path": "LINK@/href", "url": href, "rel": "alternate", "type": kind}


def wat(*records: bytes) -> bytes:
    # Like Common Crawl: one gzip member per record, concatenated.
    return b"".join(gzip.compress(r) for r in records)


def test_scan_wat_collects_every_advertised_feed() -> None:
    data = wat(
        record(b"warcinfo", warc_type="warcinfo"),
        page(
            "https://blog.example/2026/11/post/",
            [
                feed_link("/feed/"),
                feed_link("https://blog.example/comments/feed/"),
                {"url": "/style.css", "rel": "stylesheet", "type": "text/css"},
            ],
        ),
        page("https://blog.example/about/", [feed_link("https://blog.example/feed/")]),
        page("https://www.other.example/", [feed_link("rss.xml", "application/atom+xml")]),
        page("https://shop.example/", []),
        page("https://site.example/data.json", None, html=False),
        page("https://www.youtube.com/@x", [feed_link("/feeds/videos.xml?channel_id=1")]),
        page("http://bad.example/", [feed_link("ftp://bad.example/feed")]),
        record(b"{not json"),
    )
    result = scan_wat(data, PATH, frozenset({"youtube.com"}))
    assert result.pages == 6  # HTML pages only: the JSON capture is not counted
    assert result.pages_with_feed == 5
    assert result.feeds == [
        "https://blog.example/feed/",  # de-duplicated: advertised on two pages
        "https://blog.example/comments/feed/",  # no per-site selection: kept
        "https://www.other.example/rss.xml",  # relative href resolved
    ]


def test_wat_paths_keeps_only_well_formed_paths_of_the_crawl() -> None:
    listing = "\n".join(
        [PATH, "crawl-data/CC-MAIN-2026-38/segments/1/wat/x.warc.wat.gz", "../../etc/passwd", ""]
    )
    assert _wat_paths(gzip.compress(listing.encode()), CRAWL) == [PATH]
    with pytest.raises(CommonCrawlError):
        _wat_paths(gzip.compress(b"nothing useful"), CRAWL)


NOW = datetime(2026, 11, 16, tzinfo=UTC)
OLD = "CC-MAIN-2021-49"


def test_crawl_near_picks_the_crawl_that_ended_closest() -> None:
    body = json.dumps(
        [
            {"id": CRAWL, "to": "2026-10-31T00:00:00"},
            {"id": OLD, "to": "2021-12-09T00:00:00"},
            {"id": "CC-MAIN-2021-43", "to": "2021-10-28T00:00:00"},
            {"id": "CC-MAIN-2020-50", "name": "no dates"},
        ]
    ).encode()
    assert _crawl_near(body, datetime(2021, 11, 25, tzinfo=UTC)) == OLD
    with pytest.raises(CommonCrawlError):
        _crawl_near(b"[]", NOW)
    with pytest.raises(CommonCrawlError):
        _crawl_near(json.dumps([{"id": "../x", "to": "2021-01-01"}]).encode(), NOW)
    with pytest.raises(CommonCrawlError):
        _crawl_near(json.dumps([{"id": OLD, "to": "not a date"}]).encode(), NOW)


def test_complete_prefix_drops_a_record_cut_off_by_the_range() -> None:
    whole = wat(page("https://a.example/", [feed_link("/feed")]), page("https://b.example/", []))
    assert complete_prefix(whole) == whole
    cut = whole[:-10]
    first = gzip.compress(page("https://a.example/", [feed_link("/feed")]))
    assert complete_prefix(cut) == first
    assert complete_prefix(whole[:5]) == b""
    assert complete_prefix(b"") == b""
    assert scan_wat(complete_prefix(cut), PATH).feeds == ["https://a.example/feed"]


def test_complete_prefix_with_records_larger_than_a_chunk() -> None:
    big = os.urandom(200_000)  # incompressible: a gzip member of ~200 KB spans several chunks
    members = [gzip.compress(big), gzip.compress(b"small"), gzip.compress(big)]
    whole = b"".join(members)
    assert complete_prefix(whole) == whole
    assert complete_prefix(whole[:-1]) == members[0] + members[1]
    assert complete_prefix(members[0][:-1]) == b""


def test_complete_prefix_is_linear() -> None:
    # 50,000 records in 2 MB; the old version copied the remainder for each one (quadratic).
    data = b"".join(gzip.compress(b"record %d" % i) for i in range(50_000))
    started = time.perf_counter()
    assert complete_prefix(data + data[:-3]) == data + data[: data.rfind(b"\x1f\x8b")]
    assert time.perf_counter() - started < 5


def checked(
    url: str,
    days_ago: float | None,
    error: str | None = None,
    final_url: str | None = None,
    language: str | None = "en",
    title: str | None = "A Feed",
) -> CheckRow:
    date = None if days_ago is None else NOW - timedelta(days=days_ago)
    return CheckRow(
        url,
        NOW,
        final_url=final_url,
        last_item_date=date,
        error=error,
        language=language,
        title=title,
    )


def test_is_active() -> None:
    assert _is_active(checked("u", 10), NOW, 365)
    assert _is_active(checked("u", 365), NOW, 365)
    assert not _is_active(checked("u", 366), NOW, 365)
    assert not _is_active(checked("u", None), NOW, 365)  # no dated items
    assert not _is_active(checked("u", 1, error="http_5xx"), NOW, 365)
    assert _is_active(checked("u", -0.5), NOW, 365)  # time zones: up to a day ahead is fine
    assert not _is_active(checked("u", -3), NOW, 365)  # a future date is not trusted


@pytest.mark.parametrize(
    ("language", "english"),
    [
        ("en", True),
        ("de, en", True),  # English among others is enough
        ("fr", False),
        (None, False),  # nothing detected
    ],
)
def test_is_english(language: str | None, english: bool) -> None:
    assert _is_english(checked("u", 1, language=language)) is english


KEYWORDS = frozenset({"porn", "sex", "rape", "fetish"})


@pytest.mark.parametrize(
    ("title", "excluded"),
    [
        ("Captured Porn Passwords", True),
        ("BestCutScenes: death fetish", True),
        ("Sex-toys shop", True),  # hyphen and underscore separate words
        ("adult_porn", True),
        ("Essex Gardening", False),  # inside another word
        ("Pornography studies", False),  # a different word: list each form
        (None, False),
    ],
)
def test_has_keyword(title: str | None, excluded: bool) -> None:
    row = CheckRow("u", NOW, title=title)
    assert _has_keyword(row, KEYWORDS) is excluded
    assert not _has_keyword(row, frozenset())


@pytest.mark.parametrize(
    ("url", "archive"),
    [
        ("https://gazetashqiptare.al/category/bota/rajoni/feed/", True),
        ("https://x.example/tag/python/feed", True),
        ("https://x.example/author/jane/feed/atom/", True),
        ("https://x.example/blog/category/news/feed/rss2/", True),
        ("https://x.example/feed/", False),
        ("https://x.example/category/feed/", False),  # no category name: not an archive
        ("https://x.example/categories/news/feed/", False),
        ("https://x.example/tag/python/", False),  # not a feed URL
        ("https://x.example/feed/?cat=category/x/feed", False),  # the path only
    ],
)
def test_is_archive_feed(url: str, archive: bool) -> None:
    assert is_archive_feed(url) is archive


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://allsportstucson.com/feed/atom/", "https://allsportstucson.com/feed/"),
        ("https://x.example/feed/rss2", "https://x.example/feed/"),
        ("https://x.example/blog/feed/rdf/", "https://x.example/blog/feed/"),
        ("https://x.example/feed/", None),
        ("https://x.example/feed/atom.xml", None),  # not the WordPress pattern
        (
            "https://blog.jeffcable.com/feeds/posts/default?alt=rss",
            "https://blog.jeffcable.com/feeds/posts/default",
        ),
        (
            "https://x.blogspot.com/feeds/posts/default?alt=rss&max=5",
            "https://x.blogspot.com/feeds/posts/default?max=5",
        ),
        ("https://x.example/search?alt=rss", None),  # only under /feeds/
        (
            "https://ankarabasketbol.com/index.php?format=feed&type=atom",
            "https://ankarabasketbol.com/index.php?format=feed&type=rss",
        ),
        ("https://x.example/index.php?type=atom", None),  # not a Joomla feed URL
    ],
)
def test_canonical_format(url: str, expected: str | None) -> None:
    assert canonical_format(url) == (expected or url)


def test_served_url_is_https_after_redirects() -> None:
    assert served_url(checked("https://a.example/feed", 1)) == "https://a.example/feed"
    moved = checked("http://a.example/feed", 1, final_url="https://a.example/feed/")
    assert served_url(moved) == "https://a.example/feed/"
    assert served_url(checked("http://a.example/feed", 1)) is None  # still plain http
    down = checked("https://a.example/feed", 1, final_url="http://a.example/feed")
    assert served_url(down) is None
    with_sid = checked("http://f.example/feed?sid=abc", 1, final_url="https://f.example/feed?sid=x")
    assert served_url(with_sid) == "https://f.example/feed"
    to_yt = checked("https://a.example/feed", 1, final_url="https://www.youtube.com/feeds/x")
    assert served_url(to_yt, frozenset({"youtube.com"})) is None


async def test_survivors_keeps_active_english_feeds_served_over_https() -> None:
    paths = [f"crawl-data/{OLD}/segments/1/wat/CC-MAIN-1-{i:05}.warc.wat.gz" for i in range(3)]
    slice_ = wat(
        page(
            "https://s.example/",
            [feed_link("/feed/"), feed_link("/old/feed/"), feed_link("http://h.example/rss")],
        ),
        page("http://m0.example/", [feed_link("/feed")]),  # m0 and m1 redirect to one URL
        page("http://m1.example/", [feed_link("/feed")]),
        page("https://fr.example/", [feed_link("/fr/feed")]),
        page("https://s.example/a/", [feed_link("/category/news/feed/")]),  # skipped
        page("https://s.example/b/", [feed_link("/feed/atom/")]),  # folds into /feed/
        page("https://forum.example/", [feed_link("/feed?sid=1")]),  # one URL once stripped
        page("https://forum.example/x", [feed_link("/feed?sid=2")]),
        page("https://shared.example/", [feed_link("/feed/")]),
        page("https://adult.example/", [feed_link("/feed/")]),  # excluded by keyword
        page("https://untitled.example/", [feed_link("/feed/")]),  # no title
        page("https://www.youtube.com/@x", [feed_link("/feeds/videos.xml")]),
        page("https://cut.example/", [feed_link("/feed/")]),  # cut off by the range
    )
    collinfo = [
        {"id": CRAWL, "to": "2026-10-31T00:00:00"},
        {"id": OLD, "to": "2021-12-09T00:00:00"},
    ]
    served = {
        _INDEX_URL: json.dumps(collinfo).encode(),
        f"{DATA_URL}crawl-data/{OLD}/wat.paths.gz": gzip.compress("\n".join(paths).encode()),
    }
    sliced: list[str] = []

    async def fetch(url: str) -> bytes:
        return served[url]

    async def fetch_slice(url: str) -> bytes:
        sliced.append(url)
        return slice_[:-7]

    asked: list[str] = []

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        asked.extend(urls)
        rows = []
        for url in urls:
            if "/old/" in url:
                rows.append(checked(url, 400))  # newest item too old
            elif "shared" in url:
                rows.append(checked(url, None))  # no dated items
            elif "/fr/" in url:
                rows.append(checked(url, 3, language="fr"))
            elif "untitled" in url:
                rows.append(checked(url, 3, title=None))
            elif "adult" in url:
                row = checked(url, 3)
                row.title = "Free Porn Logins"
                rows.append(row)
            elif url.startswith("http://m"):  # moved to https: kept as the https target
                rows.append(checked(url, 3, final_url="https://new.example/feed"))
            elif url.startswith("http://"):
                rows.append(checked(url, 3))  # still plain http
            else:
                rows.append(checked(url, 30))
        return rows

    result = await survivors(
        fetch,
        fetch_slice,
        check,
        years_ago=5,
        active_days=365,
        exclude=frozenset({"youtube.com"}),
        keywords=KEYWORDS,
        rng=random.Random(1),
        now=NOW,
    )
    assert result.crawl == OLD
    assert len(sliced) == 1 and sliced[0].removeprefix(DATA_URL) in paths  # one random file
    assert result.scan.path == sliced[0].removeprefix(DATA_URL)
    # s/feed/, s/old/feed/, h.example/rss, m0/feed, m1/feed, fr/feed, forum/feed (sid stripped),
    # shared/feed/, adult/feed/, untitled/feed/. The category feed is never checked.
    # No YouTube, nothing from the cut record, each URL checked once.
    assert result.candidates == 10 == len(asked) == len(set(asked))
    assert result.archive_feeds == 1 and not any("/category/" in u for u in asked)
    assert "https://forum.example/feed" in asked
    assert not any("youtube" in u or "cut.example" in u for u in asked)
    assert result.working == 10
    assert result.active == 8  # s/feed/, m0, m1, h.example (http), fr, forum, adult, untitled
    assert result.english == 7  # not fr
    assert result.rejected["untitled"] == 1
    assert result.rejected["keyword"] == 1  # adult
    assert result.rejected["not_https"] == 1  # h.example: still plain http
    assert sorted(r.url for r in result.rows) == [
        "https://forum.example/feed",
        "https://new.example/feed",  # m0 and m1: one row
        "https://s.example/feed/",
    ]
    for row in result.rows:
        assert row.wat_url == sliced[0]  # credited to the WAT file it was found in
        assert row.language == "en"
        days = 3 if row.url == "https://new.example/feed" else 30  # as the fake check said
        assert row.last_item_date == NOW - timedelta(days=days)


async def test_survivors_fails_when_the_slice_fails() -> None:
    path = f"crawl-data/{OLD}/segments/1/wat/CC-MAIN-1-00000.warc.wat.gz"
    served = {
        _INDEX_URL: json.dumps([{"id": OLD, "to": "2021-12-09T00:00:00"}]).encode(),
        f"{DATA_URL}crawl-data/{OLD}/wat.paths.gz": gzip.compress(path.encode()),
    }

    async def fetch(url: str) -> bytes:
        return served[url]

    async def busy(url: str) -> bytes:
        raise CommonCrawlError("http_5xx 503")

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        raise AssertionError("nothing to check")

    with pytest.raises(CommonCrawlError, match="http_5xx 503"):  # no retries, no other file
        await survivors(fetch, busy, check, years_ago=5, active_days=365, now=NOW)


def _one_slice(feeds: int) -> tuple[Fetch, Fetch]:
    """fetch and fetch_slice serving one WAT file with `feeds` sites, each advertising a feed."""
    path = f"crawl-data/{OLD}/segments/1/wat/CC-MAIN-1-00000.warc.wat.gz"
    served = {
        _INDEX_URL: json.dumps([{"id": OLD, "to": "2021-12-09T00:00:00"}]).encode(),
        f"{DATA_URL}crawl-data/{OLD}/wat.paths.gz": gzip.compress(path.encode()),
    }
    data = wat(*(page(f"https://s{i}.example/", [feed_link("/feed")]) for i in range(feeds)))

    async def fetch(url: str) -> bytes:
        return served[url]

    async def fetch_slice(url: str) -> bytes:
        return data

    return fetch, fetch_slice


async def test_survivors_stops_once_enough_feeds_pass() -> None:
    fetch, fetch_slice = _one_slice(_CHECK_BATCH * 3)
    asked: list[str] = []

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        asked.extend(urls)
        return [checked(u, 3) for u in urls]  # every feed passes

    result = await survivors(
        fetch, fetch_slice, check, years_ago=5, active_days=365, max_feeds=10, now=NOW
    )
    assert len(result.rows) == 10
    assert len(asked) == result.checked == _CHECK_BATCH  # one batch was enough
    assert result.candidates == _CHECK_BATCH * 3


async def test_survivors_skips_known_urls_before_and_after_checking() -> None:
    fetch, fetch_slice = _one_slice(3)
    asked: list[str] = []

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        asked.extend(urls)
        # s1 now redirects to a feed that is already an input
        return [
            checked(u, 3, final_url="https://moved.example/feed" if "s1." in u else None)
            for u in urls
        ]

    async def known(urls: Sequence[str]) -> set[str]:
        return {u for u in urls if u in {"https://s0.example/feed", "https://moved.example/feed"}}

    result = await survivors(
        fetch, fetch_slice, check, years_ago=5, active_days=365, known=known, now=NOW
    )
    assert "https://s0.example/feed" not in asked  # known before checking: never fetched
    assert [r.url for r in result.rows] == ["https://s2.example/feed"]
    assert result.known == 2
