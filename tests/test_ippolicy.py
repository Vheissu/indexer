import pytest

from indexer.net.ippolicy import is_public_ip

BLOCKED = {
    # IPv4
    "unspecified": "0.0.0.0",
    "this network": "0.1.2.3",
    "private 10/8": "10.1.2.3",
    "private 172.16/12": "172.31.255.254",
    "private 192.168/16": "192.168.0.1",
    "loopback": "127.0.0.1",
    "loopback other": "127.255.0.9",
    "link-local": "169.254.1.1",
    "cloud metadata": "169.254.169.254",
    "Azure platform (WireServer)": "168.63.129.16",
    "CGNAT low": "100.64.0.1",
    "CGNAT high": "100.127.255.254",
    "IETF protocol assignments": "192.0.0.8",
    "TEST-NET-1": "192.0.2.1",
    "TEST-NET-2": "198.51.100.7",
    "TEST-NET-3": "203.0.113.9",
    "benchmarking": "198.18.0.1",
    "6to4 relay anycast": "192.88.99.1",
    "multicast": "224.0.0.1",
    "multicast high": "239.255.255.250",
    "reserved": "240.0.0.1",
    "broadcast": "255.255.255.255",
    # IPv6
    "v6 unspecified": "::",
    "v6 loopback": "::1",
    "v6 IPv4-compatible": "::127.0.0.1",
    "ULA fc00": "fc00::1",
    "ULA fd00": "fd12:3456:789a::1",
    "v6 link-local": "fe80::1",
    "v6 link-local with zone": "fe80::1%1",
    "v6 site-local": "fec0::1",
    "v6 multicast": "ff02::1",
    "v6 documentation": "2001:db8::1",
    "v6 documentation 3fff": "3fff::1",
    "Teredo": "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "discard-only": "100::1",
    "local-use NAT64": "64:ff9b:1::a00:1",
    # IPv4-mapped / NAT64 / 6to4 forms of blocked IPv4 addresses
    "mapped loopback": "::ffff:127.0.0.1",
    "mapped metadata": "::ffff:169.254.169.254",
    "mapped private": "::ffff:10.0.0.1",
    "mapped CGNAT": "::ffff:100.64.0.1",
    "mapped Azure platform": "::ffff:168.63.129.16",
    "NAT64 Azure platform": "64:ff9b::a83f:8110",
    "NAT64 loopback": "64:ff9b::7f00:1",
    "NAT64 metadata": "64:ff9b::a9fe:a9fe",
    "NAT64 private": "64:ff9b::c0a8:101",
    "6to4 private": "2002:c0a8:0101::1",
    "6to4 loopback": "2002:7f00:1::1",
    # Not an address at all
    "garbage": "not-an-ip",
}

PUBLIC = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.215.14",
    "100.63.255.255",  # just below CGNAT
    "100.128.0.0",  # just above CGNAT
    "172.32.0.1",  # just above 172.16/12
    "168.63.129.15",  # neighbours of the Azure platform address stay public
    "168.63.129.17",
    "2606:4700:4700::1111",
    "2a00:1450:4001:82b::200e",
    "::ffff:8.8.8.8",  # mapped public
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
    "2002:808:808::1",  # 6to4 of 8.8.8.8
]


@pytest.mark.parametrize("address", BLOCKED.values(), ids=BLOCKED.keys())
def test_blocked(address: str) -> None:
    assert not is_public_ip(address)


@pytest.mark.parametrize("address", PUBLIC)
def test_public(address: str) -> None:
    assert is_public_ip(address)
