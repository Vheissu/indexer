"""Shared test helpers: a fake DNS, a recording socket factory and a local feed server.

Nothing here touches the network. The local aiohttp server listens on 127.0.0.1, and the tests
use a policy in which 127.0.0.1 alone stands in for "the public internet"; every other address
is judged by the real policy. Hostnames resolve only through FakeDNS.
"""

import asyncio
import functools
import gzip
import socket
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

import pytest
from aiohappyeyeballs import AddrInfoType, SocketFactoryType
from aiohttp import web
from aiohttp.test_utils import TestServer

from indexer.net.fetch import FetchConfig, Fetcher, open_fetcher
from indexer.net.ippolicy import IPAddress, is_public_ip
from indexer.net.resolver import guarded_socket_factory

FIXTURES = Path(__file__).parent / "fixtures"
STAND_IN_PUBLIC = "127.0.0.1"


def local_policy(ip: IPAddress | str) -> bool:
    return str(ip) == STAND_IN_PUBLIC or is_public_ip(ip)


class FakeDNS:
    """Maps a hostname to a list of answers. With a list of lists, each call returns the next
    answer (the last one repeats), which is how DNS rebinding is simulated."""

    def __init__(self, records: dict[str, int | list[str] | list[list[str]]]) -> None:
        self.records = records
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        host = host.rstrip(".")
        self.calls.append(host)
        answers = self.records.get(host)
        if answers is None:
            raise socket.gaierror(socket.EAI_NONAME, f"fake DNS: unknown host {host}")
        if isinstance(answers, int):  # an EAI_* error code
            raise socket.gaierror(answers, f"fake DNS: error {answers} for {host}")
        if answers and isinstance(answers[0], list):
            seq: list[list[str]] = answers  # type: ignore[assignment]
            index = min(self.calls.count(host) - 1, len(seq) - 1)
            return seq[index]
        return answers  # type: ignore[return-value]


class RecordingSocketFactory:
    """Wraps the guarded socket factory; records every attempt and every socket created."""

    def __init__(self, policy: Callable[[IPAddress | str], bool] = local_policy) -> None:
        self._inner: SocketFactoryType = guarded_socket_factory(policy)
        self.attempted: list[str] = []
        self.created: list[str] = []

    def __call__(self, addr_info: AddrInfoType) -> socket.socket:
        address = str(addr_info[4][0])
        self.attempted.append(address)
        sock = self._inner(addr_info)
        self.created.append(address)
        return sock


@functools.cache
def make_gzip_bomb() -> bytes:
    """64 MiB of zeros, gzipped to about 64 KiB."""
    return gzip.compress(b"\0" * (64 * 1024 * 1024), compresslevel=9)


def build_app() -> web.Application:
    routes = web.RouteTableDef()
    app = web.Application()
    app["methods"] = []
    app["stats"] = {"in_flight": 0, "max_in_flight": 0}

    @routes.get("/feed.xml")
    async def feed(request: web.Request) -> web.Response:
        request.app["methods"].append(request.method)
        # Deliberately the wrong Content-Type: the parse result decides, not the header.
        return web.Response(body=_feed_bytes("rss2.xml"), content_type="text/html")

    @routes.get("/close/feed.xml")
    async def close_feed(request: web.Request) -> web.Response:
        return web.Response(
            body=_feed_bytes("rss2.xml"), headers={"Connection": "close"}, content_type="text/xml"
        )

    @routes.get("/close/redirect")
    async def close_redirect(request: web.Request) -> web.Response:
        return web.Response(
            status=302, headers={"Location": "/close/feed.xml", "Connection": "close"}
        )

    @routes.get("/chain/{n}")
    async def chain(request: web.Request) -> web.Response:
        n = int(request.match_info["n"])
        if n > 0:
            return web.Response(status=302, headers={"Location": f"../chain/{n - 1}"})
        return web.Response(body=_feed_bytes("atom.xml"))

    @routes.get("/to")
    async def to(request: web.Request) -> web.Response:
        return web.Response(status=301, headers={"Location": request.query["location"]})

    @routes.get("/no-location")
    async def no_location(request: web.Request) -> web.Response:
        return web.Response(status=302)

    @routes.get("/big")
    async def big(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()  # chunked: no Content-Length to give it away
        await resp.prepare(request)
        block = b"<" + b"x" * (256 * 1024 - 1)
        for _ in range(24):  # 6 MiB
            await resp.write(block)
        await resp.write_eof()
        return resp

    @routes.get("/big-declared")
    async def big_declared(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * (6 * 1024 * 1024))

    @routes.get("/gzip-bomb")
    async def gzip_bomb(request: web.Request) -> web.Response:
        return web.Response(body=request.app["bomb"], headers={"Content-Encoding": "gzip"})

    @routes.get("/gzip-feed")
    async def gzip_feed(request: web.Request) -> web.Response:
        body = gzip.compress(_feed_bytes("rss2.xml"))
        return web.Response(body=body, headers={"Content-Encoding": "gzip"})

    @routes.get("/brotli")
    async def brotli(request: web.Request) -> web.Response:
        return web.Response(body=b"\x0b\x02\x80", headers={"Content-Encoding": "br"})

    @routes.get("/retry-after/{code}")
    async def retry_after(request: web.Request) -> web.Response:
        return web.Response(status=int(request.match_info["code"]), headers={"Retry-After": "120"})

    @routes.get("/moved-page")
    async def moved_page(request: web.Request) -> web.Response:
        html = (
            "<!DOCTYPE html><html><head><title>Blog</title>"
            '<link rel="stylesheet" href="/s.css">'
            '<link rel="alternate" type="application/rss+xml" href="/new/feed.xml">'
            "</head><body>moved</body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    @routes.get("/status/{code}")
    async def status(request: web.Request) -> web.Response:
        return web.Response(status=int(request.match_info["code"]))

    @routes.get("/slow")
    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(5)
        return web.Response(body=_feed_bytes("rss2.xml"))

    @routes.get("/dribble")
    async def dribble(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        await resp.prepare(request)
        for _ in range(50):
            await resp.write(b" ")
            await asyncio.sleep(0.1)
        await resp.write_eof()
        return resp

    @routes.get("/huge-header")
    async def huge_header(request: web.Request) -> web.Response:
        return web.Response(body=b"<rss/>", headers={"X-Big": "a" * 20000})

    @routes.get("/many-headers")
    async def many_headers(request: web.Request) -> web.Response:
        return web.Response(body=b"<rss/>", headers={f"X-H{i}": "v" for i in range(300)})

    @routes.get("/set-cookie")
    async def set_cookie(request: web.Request) -> web.Response:
        resp = web.Response(status=302, headers={"Location": "/echo"})
        resp.set_cookie("session", "secret")
        return resp

    @routes.get("/echo")
    async def echo(request: web.Request) -> web.Response:
        lines = [f"{k}: {v}" for k, v in request.headers.items()]
        return web.Response(text="\n".join([f"METHOD: {request.method}", *lines]))

    @routes.get("/concurrent")
    async def concurrent(request: web.Request) -> web.Response:
        stats = request.app["stats"]
        stats["in_flight"] += 1
        stats["max_in_flight"] = max(stats["max_in_flight"], stats["in_flight"])
        try:
            await asyncio.sleep(0.15)
        finally:
            stats["in_flight"] -= 1
        return web.Response(body=_feed_bytes("rss2.xml"))

    @routes.get("/validators/{mode}")
    async def validators(request: web.Request) -> web.Response:
        """A feed with an ETag and Last-Modified (`ok`) or an oversized ETag (`bad`)."""
        headers = {"Last-Modified": "Mon, 01 Jan 2024 10:00:00 GMT", "ETag": 'W/"v1"'}
        if request.match_info["mode"] == "bad":
            headers = {"ETag": '"' + "x" * 2000 + '"'}
        return web.Response(body=_feed_bytes("rss2.xml"), headers=headers)

    app["bomb"] = make_gzip_bomb()
    app.add_routes(routes)
    return app


@pytest.fixture
async def server() -> AsyncIterator[TestServer]:
    srv = TestServer(build_app(), host="127.0.0.1", port=0)
    await srv.start_server()
    try:
        yield srv
    finally:
        await srv.close()


@pytest.fixture
def dns() -> FakeDNS:
    return FakeDNS(
        {
            "feeds.test": ["127.0.0.1"],
            "other.test": ["127.0.0.1"],
            "private.test": ["10.0.0.5"],
            "metadata.test": ["169.254.169.254"],
            "mixed.test": ["127.0.0.1", "192.168.1.10"],
            "rebind.test": [["127.0.0.1"], ["10.0.0.1"]],
            "nodata.test": socket.EAI_NODATA,
            "tempfail.test": socket.EAI_AGAIN,
            "servfail.test": socket.EAI_FAIL,
            **{f"h{i}.test": ["127.0.0.1"] for i in range(10)},
        }
    )


@pytest.fixture
def sockets() -> RecordingSocketFactory:
    return RecordingSocketFactory()


@pytest.fixture
def make_fetcher(
    server: TestServer, dns: FakeDNS, sockets: RecordingSocketFactory
) -> Callable[..., object]:
    """Returns an async context manager factory: `async with make_fetcher(**cfg) as f:`."""

    def factory(**overrides: object) -> object:
        overrides.setdefault("allowed_ports", {80, 443, server.port})
        config = FetchConfig(**overrides)  # type: ignore[arg-type]
        return open_fetcher(config, lookup=dns, policy=local_policy, socket_factory=sockets)

    return factory


def url(server: TestServer, path: str, host: str = "feeds.test") -> str:
    return f"http://{host}:{server.port}{path}"


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything tries real name resolution."""
    real = socket.getaddrinfo

    def guarded(host: object, *args: object, **kwargs: object) -> object:
        if host not in (None, "127.0.0.1", "::1", "localhost", "0.0.0.0", b"127.0.0.1"):
            raise AssertionError(f"test attempted real DNS lookup of {host!r}")
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


def _feed_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


__all__ = ["FakeDNS", "Fetcher", "RecordingSocketFactory", "local_policy", "url"]
