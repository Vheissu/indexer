"""Shared handling of candidate feed URLs from lists and crawls, before they become inputs."""

import re
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit, urlunsplit

from indexer.errors import CheckError
from indexer.net.domains import registrable_domain
from indexer.net.validate import validate_url

SESSION_PARAMS = frozenset({"sid", "phpsessid", "jsessionid", "sessionid", "session_id"})
_PATH_SESSION = re.compile(r";jsessionid=[^/?#]*", re.IGNORECASE)


def valid_urls(urls: Iterable[str]) -> tuple[list[tuple[str, str]], int]:
    """(canonical URL, lower-case host) pairs, in order and de-duplicated, for the URLs that
    pass the same scheme, credential, port and IP-literal rules as a check; plus how many
    did not."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    invalid = 0
    for raw in urls:
        try:
            url = validate_url(raw)
        except CheckError:
            invalid += 1
            continue
        canonical = str(url)
        if canonical not in seen:
            seen.add(canonical)
            out.append((canonical, (url.host or "").lower()))
    return out, invalid


def drop_excluded(
    valid: list[tuple[str, str]], exclude: frozenset[str]
) -> tuple[list[tuple[str, str]], int]:
    """Drop URLs whose host's registrable domain is excluded (subdomains included); return
    the rest and how many were dropped."""
    if not exclude:
        return valid, 0
    kept = [(url, host) for url, host in valid if registrable_domain(host) not in exclude]
    return kept, len(valid) - len(kept)


def strip_session(url: str) -> str:
    """The URL without session identifiers (`?sid=`, `PHPSESSID`, `;jsessionid=`, ...), which
    change per visit and would make the same feed look like a new URL. Other parameters keep
    their order and their exact encoding."""
    parts = urlsplit(url)
    path = _PATH_SESSION.sub("", parts.path)
    segments = parts.query.split("&") if parts.query else []
    kept = [p for p in segments if unquote(p.partition("=")[0]).lower() not in SESSION_PARAMS]
    query = "&".join(kept)
    if (path, query) == (parts.path, parts.query):
        return url
    return urlunsplit(parts._replace(path=path, query=query))
