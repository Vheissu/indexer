from aiohttp.test_utils import TestServer
from conftest import url

from indexer.check.runner import CheckRow, run_checks
from indexer.net.domains import registrable_domain
from indexer.net.fetch import FetchResult


class ExplodingFetcher:
    async def fetch(self, raw_url: str) -> FetchResult:
        if "boom" in raw_url:
            raise RuntimeError("bug")
        return FetchResult(url=raw_url, http_status=200, body=b"<rss><channel/></rss>")


async def test_one_row_per_url_even_when_fetcher_crashes() -> None:
    urls = ["https://a.example/feed", "https://boom.example/feed", "https://c.example/x"]
    seen: list[CheckRow] = []

    async def on_row(row: CheckRow) -> None:
        seen.append(row)

    rows = await run_checks(urls, ExplodingFetcher(), on_row=on_row)
    assert [r.url for r in rows] == urls
    assert len(seen) == 3
    assert rows[1].error == "internal_error"
    assert rows[0].error is None and rows[0].item_count == 0


async def test_end_to_end_rows(make_fetcher, server: TestServer) -> None:
    urls = [
        url(server, "/feed.xml"),
        url(server, "/chain/2"),
        url(server, "/status/404"),
        "not a url",
        "http://127.0.0.2/",
        url(server, "/gzip-bomb"),
        url(server, "/moved-page"),
    ]
    async with make_fetcher() as fetcher:
        rows = await run_checks(urls, fetcher)
    by_url = {r.url: r for r in rows}
    assert len(rows) == len(urls)

    ok = by_url[urls[0]]
    assert (ok.error, ok.item_count, ok.ttl, ok.file_size) == (None, 3, 60, ok.file_size)
    assert ok.file_size and ok.file_size > 0
    assert ok.last_item_date is not None

    atom = by_url[urls[1]]
    assert atom.error is None and atom.redirected and atom.item_count == 2

    assert by_url[urls[2]].error == "http_4xx" and by_url[urls[2]].http_status == 404
    assert by_url[urls[3]].error == "invalid_url"
    assert by_url[urls[4]].error == "blocked_ip"
    assert by_url[urls[5]].error == "too_large"

    assert (ok.feed_kind, ok.site_url, ok.ip_address) == (
        "rss",
        "https://example.com/",
        "127.0.0.1",
    )
    assert atom.feed_kind == "atom" and atom.first_redirect_status == 302

    assert by_url[urls[6]].error == "not_a_feed"


def test_registrable_domain() -> None:
    assert registrable_domain("feeds.bbci.co.uk") == "bbci.co.uk"
    assert registrable_domain("www.example.com") == "example.com"
    assert registrable_domain("alice.github.io") == "alice.github.io"
    assert registrable_domain("Example.COM.") == "example.com"
    assert registrable_domain("8.8.8.8") is None
    assert registrable_domain(None) is None
