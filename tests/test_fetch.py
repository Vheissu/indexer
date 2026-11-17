"""Fetch limits and redirect handling against a local server (no network)."""

import asyncio
import tracemalloc

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from conftest import FIXTURES, RecordingSocketFactory, url

from indexer.errors import ErrorCode
from indexer.net.fetch import USER_AGENT


async def test_success_ignores_content_type(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/feed.xml"))
    assert result.error is None, result.detail
    assert result.http_status == 200
    assert result.body == (FIXTURES / "rss2.xml").read_bytes()
    assert not result.redirected
    assert result.final_url == url(server, "/feed.xml")
    assert result.fetch_time_ms is not None and result.response_time_ms is not None
    assert server.app["methods"] == ["GET"]


async def test_three_redirects_followed_with_relative_location(
    make_fetcher,
    server: TestServer,
) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/chain/3"))
    assert result.error is None, result.detail
    assert result.redirected
    assert result.final_url == url(server, "/chain/0")
    assert len(result.hops) == 4


async def test_too_many_redirects(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/chain/4"))
    assert result.error is ErrorCode.TOO_MANY_REDIRECTS
    assert len(result.hops) == 4  # the 5th request is never made


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("http://10.0.0.1/feed", ErrorCode.BLOCKED_IP),
        ("http://169.254.169.254/latest/meta-data/", ErrorCode.BLOCKED_IP),
        ("http://[::ffff:127.0.0.2]/", ErrorCode.BLOCKED_IP),
        ("http://[::1]/", ErrorCode.BLOCKED_IP),
        ("http://private.test/feed", ErrorCode.BLOCKED_IP),
        ("http://metadata.test/latest/meta-data/", ErrorCode.BLOCKED_IP),
        ("ftp://feeds.test/feed", ErrorCode.INVALID_URL),
        ("file:///etc/passwd", ErrorCode.INVALID_URL),
        ("http://feeds.test:8080/feed", ErrorCode.INVALID_URL),
        ("http://user:pw@feeds.test/feed", ErrorCode.INVALID_URL),
    ],
)
async def test_redirect_targets_are_validated(
    make_fetcher,
    server: TestServer,
    sockets: RecordingSocketFactory,
    location: str,
    expected: ErrorCode,
) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, f"/to?location={location}"))
    assert result.error is expected, result.detail
    assert result.redirected
    assert set(sockets.created) == {"127.0.0.1"}  # only the first hop ever connected


async def test_redirect_to_other_host(make_fetcher, server: TestServer) -> None:
    target = url(server, "/feed.xml", host="other.test")
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, f"/to?location={target}"))
    assert result.error is None, result.detail
    assert result.final_url == target


async def test_redirect_without_location(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/no-location"))
    assert result.error is ErrorCode.BAD_REDIRECT


async def test_oversized_body(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/big"))
    assert result.error is ErrorCode.TOO_LARGE
    assert result.body is None


async def test_oversized_content_length(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/big-declared"))
    assert result.error is ErrorCode.TOO_LARGE


async def test_gzip_bomb_stops_at_the_cap(make_fetcher, server: TestServer) -> None:
    """64 MiB of zeros in ~64 KiB of gzip. Decoding must stop near 5 MiB, not inflate it all."""
    async with make_fetcher() as fetcher:
        tracemalloc.start()
        try:
            result = await fetcher.fetch(url(server, "/gzip-bomb"))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert result.error is ErrorCode.TOO_LARGE
    assert peak < 20 * 1024 * 1024


async def test_gzip_feed_decoded(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/gzip-feed"))
    assert result.error is None, result.detail
    assert result.body == (FIXTURES / "rss2.xml").read_bytes()


async def test_unsupported_encoding(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/brotli"))
    assert result.error is ErrorCode.UNSUPPORTED_ENCODING


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, ErrorCode.HTTP_4XX),
        (410, ErrorCode.HTTP_4XX),
        (503, ErrorCode.HTTP_5XX),
        (204, None),
        (304, ErrorCode.HTTP_OTHER),
    ],
)
async def test_status_codes(
    make_fetcher,
    server: TestServer,
    status: int,
    expected: ErrorCode | None,
) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, f"/status/{status}"))
    assert result.error is expected
    assert result.http_status == status


async def test_deadline_on_slow_headers(make_fetcher, server: TestServer) -> None:
    async with make_fetcher(deadline=0.5) as fetcher:
        result = await fetcher.fetch(url(server, "/slow"))
    assert result.error is ErrorCode.TIMEOUT
    assert result.fetch_time_ms is not None and result.fetch_time_ms < 2000

    assert result.detail == "fetch deadline exceeded"


async def test_connect_timeout_is_not_reported_as_the_deadline(
    make_fetcher, server: TestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stalled_connect(*args: object, **kwargs: object) -> object:
        raise aiohttp.ConnectionTimeoutError("Connection timeout to host")

    async with make_fetcher() as fetcher:
        monkeypatch.setattr(fetcher._session, "get", stalled_connect)
        result = await fetcher.fetch(url(server, "/feed.xml"))
    assert result.error is ErrorCode.TIMEOUT
    assert result.detail and result.detail.startswith("ConnectionTimeoutError")


async def test_deadline_covers_body(make_fetcher, server: TestServer) -> None:
    async with make_fetcher(deadline=0.5) as fetcher:
        result = await fetcher.fetch(url(server, "/dribble"))
    assert result.error is ErrorCode.TIMEOUT
    assert result.http_status == 200


@pytest.mark.parametrize("path", ["/huge-header", "/many-headers"])
async def test_header_limits(make_fetcher, server: TestServer, path: str) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, path))
    assert result.error is ErrorCode.HEADER_TOO_LARGE, result.detail


async def test_no_cookies_and_honest_user_agent(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/set-cookie"))
    assert result.body is not None
    echoed = result.body.decode()
    assert "METHOD: GET" in echoed
    assert "Cookie" not in echoed
    assert f"User-Agent: {USER_AGENT}" in echoed
    assert "indexer" in USER_AGENT


async def test_proxy_environment_ignored(
    make_fetcher,
    server: TestServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://10.255.255.1:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/feed.xml"))
    assert result.error is None, result.detail


async def test_connection_refused(make_fetcher, server: TestServer) -> None:
    async with make_fetcher(allowed_ports={80}) as fetcher:
        result = await fetcher.fetch("http://feeds.test/feed.xml")  # nothing on port 80
    assert result.error is ErrorCode.CONNECT_ERROR, result.detail
    assert result.ip_address == "127.0.0.1"  # the address the failed connection went to


async def test_per_host_limit(make_fetcher, server: TestServer) -> None:
    async with make_fetcher(per_host=2, concurrency=20) as fetcher:
        results = await asyncio.gather(
            *(fetcher.fetch(url(server, f"/concurrent?i={i}")) for i in range(8))
        )
    assert all(r.error is None for r in results)
    assert server.app["stats"]["max_in_flight"] == 2


async def test_global_limit(make_fetcher, server: TestServer) -> None:
    async with make_fetcher(per_host=2, concurrency=3) as fetcher:
        results = await asyncio.gather(
            *(fetcher.fetch(url(server, "/concurrent", host=f"h{i}.test")) for i in range(9))
        )
    assert all(r.error is None for r in results)
    assert server.app["stats"]["max_in_flight"] == 3


async def test_peer_address_recorded(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/feed.xml"))
    assert result.ip_address == "127.0.0.1"


@pytest.mark.parametrize(
    ("path", "first"),
    [("/chain/3", 302), ("/to?location=/feed.xml", 301), ("/feed.xml", None)],
)
async def test_first_redirect_status(
    make_fetcher, server: TestServer, path: str, first: int | None
) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, path))
    assert result.error is None, result.detail
    assert result.first_redirect_status == first


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/status/429", ErrorCode.RATE_LIMITED),
        ("/retry-after/429", ErrorCode.RATE_LIMITED),
        ("/retry-after/503", ErrorCode.RATE_LIMITED),
        ("/status/503", ErrorCode.HTTP_5XX),
        ("/status/410", ErrorCode.HTTP_4XX),
    ],
)
async def test_rate_limited(
    make_fetcher, server: TestServer, path: str, expected: ErrorCode
) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, path))
    assert result.error is expected


async def test_extra_headers_go_to_the_original_host_only(make_fetcher, server: TestServer) -> None:
    """Extra headers (e.g. Range) are never forwarded to another host on a redirect."""
    extra = {"X-Extra": "secret-value"}
    async with make_fetcher() as fetcher:
        same = await fetcher.fetch(url(server, "/echo"), extra)
        other_echo = url(server, "/echo", host="other.test")
        moved = await fetcher.fetch(url(server, f"/to?location={other_echo}"), extra)
        relative = await fetcher.fetch(url(server, "/to?location=/echo"), extra)
    assert same.body is not None and b"X-Extra: secret-value" in same.body
    assert moved.redirected and moved.body is not None
    assert b"X-Extra" not in moved.body
    assert relative.body is not None and b"X-Extra: secret-value" in relative.body


async def test_validators_are_stored_as_sent(make_fetcher, server: TestServer) -> None:
    async with make_fetcher() as fetcher:
        ok = await fetcher.fetch(url(server, "/validators/ok"))
        bad = await fetcher.fetch(url(server, "/validators/bad"))
    assert (ok.etag, ok.last_modified) == ('W/"v1"', "Mon, 01 Jan 2024 10:00:00 GMT")
    assert bad.error is None and bad.etag is None  # oversized: not stored
