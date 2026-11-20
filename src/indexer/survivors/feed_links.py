"""Offline filtering of the feed links a crawled page advertises: URL rules, nothing fetched."""

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from indexer.net.urls import drop_excluded, strip_session, valid_urls

FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/rdf+xml"})

# WordPress archive feeds: a category, tag or author subset of the site's main feed.
_ARCHIVE_FEED = re.compile(r"/(?:category|tag|author)/.+/feed(?:/(?:rss2?|atom|rdf))?/?$", re.I)
# Comment feeds: WordPress /comments/feed/ (and its formats), Blogger /feeds/comments/... and
# /feeds/<post id>/comments/...
_COMMENT_FEED = re.compile(
    r"/comments/feed(?:/(?:rss2?|atom|rdf))?/?$|^/feeds/(?:\d+/)?comments/", re.I
)
# WordPress without pretty links: ?feed=comments-rss2, ?feed=comments-atom
_COMMENT_QUERY = re.compile(r"(?:^|&)feed=comments-", re.I)


def filter_page_links(page: str, links: Any, exclude: frozenset[str] = frozenset()) -> list[str]:
    """The feed URLs worth checking among a page's `<link>` elements (as the WAT records them):
    rel="alternate" with a feed type, made absolute, valid like any input, outside the `exclude`
    domains, not an archive or comment feed, without session parameters."""
    urls: list[str] = []
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel", "")).lower().split()
        kind = str(link.get("type", "")).split(";")[0].strip().lower()
        href = link.get("url")
        if "alternate" in rel and kind in FEED_TYPES and isinstance(href, str) and href.strip():
            try:
                urls.append(urljoin(page, href.strip()))
            except ValueError:
                continue
    valid, _invalid = valid_urls(urls)
    valid, _excluded = drop_excluded(valid, exclude)
    return [
        strip_session(url)
        for url, _host in valid
        if not is_archive_feed(url) and not is_comment_feed(url)
    ]


def is_archive_feed(url: str) -> bool:
    """A WordPress category, tag or author feed (`/category/news/feed/`): a subset of the main
    feed."""
    return bool(_ARCHIVE_FEED.search(urlsplit(url).path))


def is_comment_feed(url: str) -> bool:
    """A feed of comments rather than posts (`/comments/feed/`, Blogger `/feeds/comments/`)."""
    parts = urlsplit(url)
    return bool(_COMMENT_FEED.search(parts.path) or _COMMENT_QUERY.search(parts.query))
