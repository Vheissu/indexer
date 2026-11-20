"""Which Common Crawl feeds survivors proposes: the rules agreed with the user (see CLAUDE.md).

Before checking, by URL alone:
    is_archive_feed   WordPress category / tag / author feeds are skipped
    is_comment_feed   comment feeds (WordPress, Blogger) are skipped

After checking, `rejection(row, rules)` gives the first rule a feed fails, or None if it passes:
    failed       the check failed (fetch or parse)
    inactive     no item from the last `active_days` days
    not_english  English is not among the languages detected on its items
    untitled     the feed has no title
    keyword      its title contains an excluded keyword (`[commoncrawl] exclude_keywords`)
    not_https    not served over https today (or redirected to an excluded domain)
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from indexer.check.runner import CheckRow
from indexer.net.urls import drop_excluded, strip_session, valid_urls

type Reason = Literal["failed", "inactive", "not_english", "untitled", "keyword", "not_https"]

# WordPress archive feeds: a category, tag or author subset of the site's main feed.
_ARCHIVE_FEED = re.compile(r"/(?:category|tag|author)/.+/feed(?:/(?:rss2?|atom|rdf))?/?$", re.I)
# Comment feeds: WordPress /comments/feed/ (and its formats), Blogger /feeds/comments/... and
# /feeds/<post id>/comments/...
_COMMENT_FEED = re.compile(
    r"/comments/feed(?:/(?:rss2?|atom|rdf))?/?$|^/feeds/(?:\d+/)?comments/", re.I
)
# WordPress without pretty links: ?feed=comments-rss2, ?feed=comments-atom
_COMMENT_QUERY = re.compile(r"(?:^|&)feed=comments-", re.I)


@dataclass(frozen=True, slots=True)
class Rules:
    now: datetime
    active_days: int  # the newest item must be at most this old
    exclude: frozenset[str] = frozenset()  # excluded domains (`[exclude] domains`)
    keywords: frozenset[str] = frozenset()  # excluded title words, lower case


def rejection(row: CheckRow, rules: Rules) -> Reason | None:
    """The first rule the checked feed fails, or None if it passes them all."""
    if row.error is not None:
        return "failed"
    if not _is_active(row, rules.now, rules.active_days):
        return "inactive"
    if not _is_english(row):
        return "not_english"
    if not row.title:
        return "untitled"
    if _has_keyword(row, rules.keywords):
        return "keyword"
    if served_url(row, rules.exclude) is None:
        return "not_https"
    return None


# --- the rules after checking ---------------------------------------------------------------


def _is_active(row: CheckRow, now: datetime, active_days: int) -> bool:
    """Parsed today, and the newest item is from the last `active_days` days. A date more than a
    day in the future is not trusted."""
    date = row.last_item_date
    if row.error is not None or date is None:
        return False
    return now - timedelta(days=active_days) <= date <= now + timedelta(days=1)


def _is_english(row: CheckRow) -> bool:
    """English is among the languages detected on the feed's items (`en`, see
    `indexer.language`). What the feed declares is not read."""
    return "en" in {t.strip() for t in (row.language or "").split(",")}


def _has_keyword(row: CheckRow, keywords: frozenset[str]) -> bool:
    """The feed's title contains one of `keywords` (lower case) as a whole word: not inside
    another word, so "sex" matches "Sex Toys" and "sex-ed" but not "Essex"."""
    if not keywords:
        return False
    words = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    pattern = re.compile(rf"(?<![^\W_])(?:{words})(?![^\W_])")
    return pattern.search((row.title or "").lower()) is not None


def served_url(row: CheckRow, exclude: frozenset[str] = frozenset()) -> str | None:
    """The URL the feed is served from today (after redirects, session parameters removed) if
    it is a valid https URL
    outside the `exclude` domains (a redirect can lead anywhere); else None."""
    valid, _invalid = valid_urls([strip_session(row.final_url or row.url)])
    valid, _excluded = drop_excluded(valid, exclude)
    if not valid or not valid[0][0].startswith("https://"):
        return None
    return valid[0][0]


# --- the rules before checking --------------------------------------------------------------


def is_archive_feed(url: str) -> bool:
    """A WordPress category, tag or author feed (`/category/news/feed/`): a subset of the main
    feed."""
    return bool(_ARCHIVE_FEED.search(urlsplit(url).path))


def is_comment_feed(url: str) -> bool:
    """A feed of comments rather than posts (`/comments/feed/`, Blogger `/feeds/comments/`)."""
    parts = urlsplit(url)
    return bool(_COMMENT_FEED.search(parts.path) or _COMMENT_QUERY.search(parts.query))
