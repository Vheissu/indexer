"""Registrable ("parent") domain from the public suffix list bundled with publicsuffixlist.

The list ships inside the package; nothing is downloaded at run time. Private-section suffixes
(github.io, blogspot.com, ...) count as suffixes, so alice.github.io is its own domain.
"""

import ipaddress
from functools import lru_cache

from publicsuffixlist import PublicSuffixList
from yarl import URL


@lru_cache(maxsize=1)
def _psl() -> PublicSuffixList:
    return PublicSuffixList()


def registrable_domain(host: str | None) -> str | None:
    if not host:
        return None
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    result: str | None = _psl().privatesuffix(host)
    return result


def parent_domain_of(url: str) -> str | None:
    try:
        return registrable_domain(URL(url).host)
    except (ValueError, TypeError, UnicodeError):
        return None
