"""DNS-rebinding-safe address checks, plugged into aiohttp's connector.

aiohttp's TCPConnector asks its resolver for addresses and then connects only to the addresses
it returns, sending the original hostname as TLS SNI and verifying the certificate against it.
GuardedResolver does the one lookup per connection and rejects the host if any answer is
non-public, so the checked addresses are exactly the addresses the socket connects to. The
connector must run with use_dns_cache=False so every new connection goes through it.

guarded_socket_factory is a second layer: aiohttp calls it with the final address right before
creating each socket, so even an address that skipped the resolver cannot be connected to.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence

from aiohappyeyeballs import AddrInfoType, SocketFactoryType
from aiohttp.abc import AbstractResolver, ResolveResult

from indexer.net.ippolicy import is_public_ip
from indexer.net.validate import IPPolicy

BLOCKED_MARKER = "indexer-blocked:"

type Lookup = Callable[[str, int], Awaitable[Sequence[str]]]


class BlockedAddressError(OSError):
    """A host resolved to (or a socket targeted) a non-public address."""

    def __init__(self, host: str, addresses: Sequence[str]) -> None:
        super().__init__(
            f"{BLOCKED_MARKER} {host} -> non-public address(es): {', '.join(addresses)}"
        )
        self.host = host
        self.addresses = list(addresses)


async def system_lookup(host: str, port: int) -> list[str]:
    """Resolve with the system resolver; return unique addresses in the order given."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for *_, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class GuardedResolver(AbstractResolver):
    def __init__(self, lookup: Lookup = system_lookup, policy: IPPolicy = is_public_ip) -> None:
        self._lookup = lookup
        self._policy = policy

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_UNSPEC
    ) -> list[ResolveResult]:
        addresses = list(await self._lookup(host, port))
        if not addresses:
            raise socket.gaierror(socket.EAI_NONAME, f"no addresses for {host}")
        blocked = [a for a in addresses if not self._policy(a)]
        if blocked:
            raise BlockedAddressError(host, blocked)
        results: list[ResolveResult] = []
        for address in addresses:
            fam = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, fam):
                continue
            results.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=port,
                    family=fam,
                    proto=0,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        if not results:
            raise socket.gaierror(socket.EAI_ADDRFAMILY, f"no usable addresses for {host}")
        return results

    async def close(self) -> None:
        return None


def guarded_socket_factory(policy: IPPolicy = is_public_ip) -> SocketFactoryType:
    def factory(addr_info: AddrInfoType) -> socket.socket:
        family, type_, proto, _canonname, sockaddr = addr_info
        address = str(sockaddr[0])
        if not policy(address):
            raise BlockedAddressError("socket", [address])
        return socket.socket(family=family, type=type_, proto=proto)

    return factory
