"""The guarded resolver and socket factory, including the DNS-rebinding (TOCTOU) case."""

import socket

import pytest
from aiohttp.test_utils import TestServer
from conftest import FakeDNS, RecordingSocketFactory, local_policy, url

from indexer.errors import ErrorCode
from indexer.net.fetch import FetchConfig, open_fetcher
from indexer.net.resolver import BlockedAddressError, GuardedResolver


async def test_resolver_returns_checked_addresses() -> None:
    resolver = GuardedResolver(FakeDNS({"a.test": ["8.8.8.8", "2606:4700:4700::1111"]}))
    results = await resolver.resolve("a.test", 443)
    assert [(r["host"], r["family"], r["port"]) for r in results] == [
        ("8.8.8.8", socket.AF_INET, 443),
        ("2606:4700:4700::1111", socket.AF_INET6, 443),
    ]
    assert all(r["hostname"] == "a.test" for r in results)


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["10.0.0.1"],
        ["8.8.8.8", "192.168.0.1"],  # one bad answer poisons the whole host
        ["fd00::1"],
        ["::ffff:127.0.0.1"],
    ],
)
async def test_resolver_rejects_any_non_public_answer(answers: list[str]) -> None:
    resolver = GuardedResolver(FakeDNS({"a.test": answers}))
    with pytest.raises(BlockedAddressError):
        await resolver.resolve("a.test", 80)


async def test_resolver_rebinding_second_answer_rejected() -> None:
    """Each lookup is checked on its own result: public first, private next time."""
    dns = FakeDNS({"rebind.test": [["8.8.8.8"], ["127.0.0.1"]]})
    resolver = GuardedResolver(dns)
    assert [r["host"] for r in await resolver.resolve("rebind.test", 80)] == ["8.8.8.8"]
    with pytest.raises(BlockedAddressError):
        await resolver.resolve("rebind.test", 80)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("nxdomain.test", ErrorCode.DNS_NXDOMAIN),  # EAI_NONAME
        ("nodata.test", ErrorCode.DNS_NXDOMAIN),  # the name exists but has no addresses
        ("tempfail.test", ErrorCode.DNS_TEMPORARY),  # EAI_AGAIN
        ("servfail.test", ErrorCode.DNS_ERROR),  # EAI_FAIL
    ],
)
async def test_dns_failures(
    make_fetcher, server: TestServer, host: str, expected: ErrorCode
) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/feed.xml", host=host))
    assert result.error is expected, result.detail
    assert result.ip_address is None


async def test_rebinding_across_fetches_never_connects_to_private_ip(
    make_fetcher,
    server: TestServer,
    dns: FakeDNS,
    sockets: RecordingSocketFactory,
) -> None:
    """rebind.test answers 127.0.0.1 (stand-in public) first, then 10.0.0.1. The server closes
    each connection, so the second fetch must open (and resolve) a new one."""
    async with make_fetcher() as fetcher:
        first = await fetcher.fetch(url(server, "/close/feed.xml", host="rebind.test"))
        second = await fetcher.fetch(url(server, "/close/feed.xml", host="rebind.test"))
    assert first.error is None
    assert second.error is ErrorCode.BLOCKED_IP
    assert dns.calls.count("rebind.test") == 2
    assert "10.0.0.1" not in sockets.attempted
    assert sockets.created == ["127.0.0.1"]


async def test_rebinding_within_one_fetch_on_redirect(
    make_fetcher,
    server: TestServer,
    dns: FakeDNS,
    sockets: RecordingSocketFactory,
) -> None:
    """The first hop resolves to the stand-in public IP and redirects to the same host on a
    closed connection; the second hop's lookup returns a private IP and is refused."""
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/close/redirect", host="rebind.test"))
    assert result.error is ErrorCode.BLOCKED_IP
    assert result.redirected
    assert dns.calls.count("rebind.test") == 2
    assert "10.0.0.1" not in sockets.attempted
    assert sockets.created == ["127.0.0.1"]


async def test_socket_factory_blocks_even_if_resolver_lets_it_through(
    server: TestServer,
) -> None:
    """Defence in depth: with a resolver that allows everything, the socket factory still
    refuses to create a socket for a private address."""
    sockets = RecordingSocketFactory(policy=local_policy)
    config = FetchConfig(allowed_ports={80, 443, server.port})
    dns = FakeDNS({"private.test": ["10.0.0.7"]})
    # The resolver policy allows everything; the socket factory uses the real (local) policy.
    async with open_fetcher(
        config, lookup=dns, policy=lambda _ip: True, socket_factory=sockets
    ) as fetcher:
        result = await fetcher.fetch(url(server, "/feed.xml", host="private.test"))
    assert result.error is ErrorCode.BLOCKED_IP
    assert result.detail == "socket -> non-public address(es): 10.0.0.7"
    assert sockets.attempted == ["10.0.0.7"]
    assert sockets.created == []


@pytest.mark.parametrize(
    ("host", "refused"),
    [
        ("private.test", "10.0.0.5"),
        ("metadata.test", "169.254.169.254"),
        ("mixed.test", "192.168.1.10"),
    ],
)
async def test_hostname_resolving_to_private_ip_blocked(
    make_fetcher,
    server: TestServer,
    sockets: RecordingSocketFactory,
    host: str,
    refused: str,
) -> None:
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch(url(server, "/feed.xml", host=host))
    assert result.error is ErrorCode.BLOCKED_IP
    assert sockets.attempted == []
    # the detail names the host and only the refused address(es)
    assert result.detail == f"{host} -> non-public address(es): {refused}"
