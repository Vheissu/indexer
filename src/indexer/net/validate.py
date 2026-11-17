"""Reading the URL list and validating each URL (and each redirect target)."""

import ipaddress
from collections.abc import Callable, Collection
from pathlib import Path
from urllib.parse import urlsplit

from yarl import URL

from indexer.errors import CheckError, ErrorCode
from indexer.net.ippolicy import IPAddress, is_public_ip

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})

type IPPolicy = Callable[[IPAddress | str], bool]


def read_url_file(path: Path) -> list[str]:
    """One URL per line; blank lines and lines starting with # are skipped; duplicates dropped."""
    seen: set[str] = set()
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#") or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def validate_url(
    raw: str | URL,
    *,
    allowed_ports: Collection[int] = DEFAULT_ALLOWED_PORTS,
    policy: IPPolicy = is_public_ip,
) -> URL:
    """Return the parsed URL, or raise CheckError (invalid_url or blocked_ip).

    Hostnames are checked later, at connect time, by the guarded resolver. An IP-literal host
    never reaches the resolver, so it is checked here.
    """
    try:
        if isinstance(raw, str):
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
                raise CheckError(ErrorCode.INVALID_URL, "whitespace or control character")
            if "@" in urlsplit(raw).netloc:
                # Checked on the raw text too: yarl drops an empty userinfo ("http://@host/").
                raise CheckError(ErrorCode.INVALID_URL, "credentials in URL")
            url = URL(raw)
        else:
            url = raw
        if url.scheme not in ALLOWED_SCHEMES:
            raise CheckError(ErrorCode.INVALID_URL, f"scheme {url.scheme!r} not allowed")
        if not url.host:
            raise CheckError(ErrorCode.INVALID_URL, "missing host")
        if url.user is not None or url.password is not None or "@" in (url.raw_authority or ""):
            raise CheckError(ErrorCode.INVALID_URL, "credentials in URL")
        port = url.explicit_port
        if port is not None and port not in allowed_ports:
            raise CheckError(ErrorCode.INVALID_URL, f"port {port} not allowed")
        host = url.host
    except CheckError:
        raise
    except (ValueError, TypeError, UnicodeError) as exc:
        raise CheckError(ErrorCode.INVALID_URL, str(exc) or type(exc).__name__) from exc

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not policy(literal):
        raise CheckError(ErrorCode.BLOCKED_IP, f"{literal} is not a public address")
    return url.with_fragment(None)
