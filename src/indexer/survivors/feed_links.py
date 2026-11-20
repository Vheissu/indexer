"""Offline filtering of the feed links a crawled page advertises: URL rules, nothing fetched."""

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from indexer.net.urls import drop_excluded, strip_session, valid_urls

FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/rdf+xml"})

MAX_LENGTH = 256  # characters in the URL
MAX_SLASHES = 7  # in the path
MAX_SEGMENT = 25  # characters in the longest path segment

# Somewhere in the URL.
_FEED_INDICATORS = re.compile(
    r"(atom|rss|feed|rdf|xml|spip|synd|pod|api|backend\.php)", re.IGNORECASE
)
# Anywhere in the URL: never a feed worth proposing.
_REJECTED = [
    re.compile(r"/20[0-9]{2}/[0-9]{2}/"),  # a dated post
    re.compile(r"/comments"),
    re.compile(r"/sitemap\.xml"),
    re.compile(r"comments/"),
    re.compile(r"feed=comments"),
    re.compile(r"commentId="),
    re.compile(r"/comment/"),
    re.compile(r":Recent"),
    re.compile(r"alt=rss"),
]
# The whole of what follows the host must be one of these.
_ACCEPTED = [
    re.compile(r"/(atom|rss|rss2|feed|feeds)(\.feed\.php)?/?"),
    re.compile(r"/(atom|rss|rss2|feed|feeds)\.xml"),
    re.compile(r"/recent\.rss"),
    re.compile(r"/\?rss=1"),
    re.compile(r"\?com=rss"),
    re.compile(r"/(atom|rss|rss2|feed|feeds)\.php"),
    re.compile(r"/xml\.php\?rss=true"),
    re.compile(r"/app\.php/feed"),
    re.compile(r"/(atom|rss|feed|feeds)(/|\.)[a-zA-Z0-9_.-]+/?"),
    re.compile(r"/[a-zA-Z0-9_.-]+(/|\.)(atom|rss|feed|feeds)/?"),
]


def filter_page_links(page: str, links: Any, exclude: frozenset[str] = frozenset()) -> list[str]:
    """The feed URLs worth checking among a page's `<link>` elements (as the WAT records them):
    rel="alternate" with a feed type, made absolute, on https, valid like any input, outside the
    `exclude` domains, without session parameters, and shaped like a feed URL
    (`_looks_like_a_feed`; that also rules out comment and WordPress archive feeds)."""
    urls: list[str] = []
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel", "")).lower().split()
        kind = str(link.get("type", "")).split(";")[0].strip().lower()
        href = link.get("url")
        if "alternate" in rel and kind in FEED_TYPES and isinstance(href, str) and href.strip():
            try:
                url = urljoin(page, href.strip())
            except ValueError:
                continue
            if url.lower().startswith("http://"):  # tried on https; other schemes stay invalid
                url = "https://" + url[len("http://") :]
            urls.append(url)
    valid, _invalid = valid_urls(urls)
    valid, _excluded = drop_excluded(valid, exclude)
    kept = []
    for url, _host in valid:
        url = strip_session(url)
        if _looks_like_a_feed(url):
            kept.append(url)
    return kept


def _looks_like_a_feed(url: str) -> bool:
    """Short, not too deep, no rejected pattern, and what follows the host is an accepted shape."""
    parts = urlsplit(url)
    if len(url) > MAX_LENGTH or not _FEED_INDICATORS.search(url):
        return False
    if any(pattern.search(url) for pattern in _REJECTED):
        return False
    if parts.path.count("/") > MAX_SLASHES:
        return False
    if max(len(segment) for segment in parts.path.split("/")) > MAX_SEGMENT:
        return False
    beyond_host = url[len(parts.scheme) + 3 + len(parts.netloc) :]
    return any(pattern.fullmatch(beyond_host) for pattern in _ACCEPTED)
