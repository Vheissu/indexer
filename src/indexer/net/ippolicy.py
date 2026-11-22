"""Decide whether an IP address is public, i.e. safe to connect to.

Explicit deny lists are checked first; `is_global` is required as well, so an address must
pass both. IPv6 forms that embed an IPv4 address (IPv4-mapped, NAT64, 6to4) are judged by the
embedded IPv4 address.
"""

import ipaddress
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

type IPAddress = IPv4Address | IPv6Address

BLOCKED_V4: tuple[IPv4Network, ...] = tuple(
    IPv4Network(n)
    for n in (
        "0.0.0.0/8",  # "this network", includes the unspecified address
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # CGNAT shared address space
        # Azure platform address (DNS, DHCP, health probes, the VM agent's WireServer).
        # Public-looking, but on an Azure VM such as a GitHub-hosted runner it reaches the
        # host platform, so it is as internal as the metadata service.
        "168.63.129.16/32",
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, includes cloud metadata 169.254.169.254
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # deprecated 6to4 relay anycast
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, includes broadcast 255.255.255.255
    )
)

BLOCKED_V6: tuple[IPv6Network, ...] = tuple(
    IPv6Network(n)
    for n in (
        "::/96",  # unspecified, and deprecated IPv4-compatible addresses
        "::1/128",  # loopback
        "64:ff9b:1::/48",  # local-use NAT64 (RFC 8215)
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments, includes Teredo 2001::/32
        "2001:db8::/32",  # documentation
        "3fff::/20",  # documentation
        "5f00::/16",  # SRv6 SIDs
        "fc00::/7",  # unique local addresses (ULA)
        "fe80::/10",  # link-local
        "fec0::/10",  # deprecated site-local
        "ff00::/8",  # multicast
    )
)

NAT64_WELL_KNOWN = IPv6Network("64:ff9b::/96")


def embedded_ipv4(ip: IPv6Address) -> IPv4Address | None:
    """Return the IPv4 address carried inside an IPv4-mapped, NAT64 or 6to4 address."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in NAT64_WELL_KNOWN:
        return IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    return None


def is_public_ip(ip: IPAddress | str) -> bool:
    """True only for globally routable unicast addresses. Unparseable input is not public."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip)
        except ValueError:
            return False
    if isinstance(ip, IPv4Address):
        return _is_public_v4(ip)
    inner = embedded_ipv4(ip)
    if inner is not None:
        return _is_public_v4(inner)
    if ip.scope_id:
        return False
    if any(ip in net for net in BLOCKED_V6):
        return False
    return ip.is_global and not ip.is_multicast


def _is_public_v4(ip: IPv4Address) -> bool:
    if any(ip in net for net in BLOCKED_V4):
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved
