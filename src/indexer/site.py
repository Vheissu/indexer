"""The published website: static HTML pages in output/docs/, served by GitHub Pages from the
output branch's /docs folder. An index of the tags and one page per tag (its most recently
active feeds), a stylesheet, feedrule.com's icon, a sitemap and .nojekyll. The data files
(links.txt, OPML, previews) stay outside docs/ and are linked on raw.githubusercontent.com,
so they don't count toward the Pages size limit.

The pages are Jinja templates (src/indexer/templates/) with autoescaping on, so every value from
a feed is escaped unless marked safe (nothing is). Escaping does not make a URL safe, so a link
is only written for an http(s) URL (`safe_href`: a feed's site URL could be `javascript:`).
Links to the feeds and their sites are rel="nofollow"; links to feedrule.com are not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import quote, urlsplit

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

if TYPE_CHECKING:
    from indexer.export import ListedFeed, Tag

PAGE_ROWS = 200  # feeds shown on a tag page (the first ones of its feeds.opml)
FEED_TAGS = 10  # tags shown under each feed on a tag page
INDEX_TAGS = 50  # topics shown on the home page (the filter box searches all of them)
FILTER_MATCHES = 100  # topics the filter box shows at most
FEEDRULE = "https://feedrule.com/"
FEEDRULE_FORM = "https://feedrule.com/?feed="  # the email-digest form, prefilled with a feed
REPO = "https://github.com/feedrule/indexer"
ICON = "feedrule.svg"
STYLE = "style.css"


class Source(NamedTuple):
    name: str
    url: str
    license: str
    license_url: str  # or ""
    feeds: str  # what it has
    how: str  # a "how" link, or ""


# The source lists and their licenses, shown on the index. Update with data/sources.toml.
SOURCES = (
    Source("kagisearch/smallweb", "https://github.com/kagisearch/smallweb",
           "MIT", "", "~44,000 personal sites", ""),
    Source("outcoldman/hackernews-personal-blogs",
           "https://github.com/outcoldman/hackernews-personal-blogs",
           "MIT", "", "~1,230 Hacker News users' blogs", ""),
    Source("simevidas/web-dev-feeds", "https://github.com/simevidas/web-dev-feeds",
           "MIT", "", "~1,070 web development blogs", ""),
    Source("plenaryapp/awesome-rss-feeds", "https://github.com/plenaryapp/awesome-rss-feeds",
           "CC0-1.0", "", "~750 by topic, and news sites by country", ""),
    Source("tuan3w/awesome-tech-rss", "https://github.com/tuan3w/awesome-tech-rss",
           "CC0-1.0", "", "~140 startups, science, technology", ""),
    Source("matthiasjost/dotnet-creators-opml",
           "https://github.com/matthiasjost/dotnet-creators-opml",
           "MIT", "", "~140 .NET blogs", ""),
    Source("data/survivors.txt", f"{REPO}/blob/develop/data/survivors.txt",
           "Common Crawl terms", "https://commoncrawl.org/terms-of-use",
           "old feeds still active today, reviewed by hand", f"{REPO}#feeds-from-common-crawl"),
    Source("data/submissions.txt", f"{REPO}/blob/develop/data/submissions.txt",
           "MIT", "", "feeds submitted to the project", ""),
)  # fmt: skip

_env = Environment(
    loader=PackageLoader("indexer", "templates"),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=StrictUndefined,  # a typo in a template fails the export instead of rendering ""
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_env.filters["num"] = lambda n: f"{n:,}"
_env.policies["json.dumps_kwargs"] = {"separators": (",", ":")}  # compact topic list
_env.globals.update(icon=ICON, style=STYLE, feedrule=FEEDRULE, repo=REPO)


def safe_href(url: str | None) -> str | None:
    """An untrusted URL usable as a link (the template escapes it), or None unless it is
    http(s)."""
    if not url or urlsplit(url.strip()).scheme.lower() not in ("http", "https"):
        return None
    return url.strip()


def feedrule_href(url: str) -> str:
    """feedrule.com's form prefilled with the feed `url`, percent-encoded (only ':' and '/'
    kept) so it stays one query value."""
    return FEEDRULE_FORM + quote(url, safe=":/")


def feed_tags(
    feed: ListedFeed,
    current: str,
    counts: Mapping[str, int],
    published: frozenset[str],
    ignore: frozenset[str] = frozenset(),
) -> list[tuple[str, bool]]:
    """Up to FEED_TAGS of the feed's tags other than `current`, the most widespread first (by
    number of listed feeds: tag frequency inside a feed is not stored), each with whether it
    has a page to link to."""
    slugs = sorted(feed.tags(ignore) - {current}, key=lambda t: (-counts.get(t, 0), t))
    return [(t, t in published) for t in slugs[:FEED_TAGS]]


def _row(
    feed: ListedFeed,
    current: str,
    counts: Mapping[str, int],
    published: frozenset[str],
    ignore: frozenset[str],
) -> dict[str, object]:
    parts = urlsplit(feed.url)
    # the feed's website, or else the home page of the feed's own domain
    site = safe_href(feed.site_url) or safe_href(f"{parts.scheme}://{parts.netloc}/") or ""
    return {
        "label": " ".join((feed.title or "").split()) or feed.url,
        "feed": safe_href(feed.url) or "",
        "site": site,
        "host": urlsplit(site).hostname or "",
        "feedrule": feedrule_href(feed.url),
        "tags": feed_tags(feed, current, counts, published, ignore),
        "date": feed.last_item_date.date().isoformat() if feed.last_item_date else "",
    }


def render_tag_page(
    tag: Tag,
    generated: datetime,
    *,
    site_url: str,
    data_url: str,
    opml_max: int,
    links_max: int,
    counts: Mapping[str, int] | None = None,
    published: frozenset[str] = frozenset(),
    ignore: frozenset[str] = frozenset(),
) -> str:
    """`counts` (listed feeds per tag) orders each feed's tags; `published` are the tags with a
    page; `ignore` are left out."""
    total = len(tag.feeds)
    return _env.get_template("tag.html").render(
        title=f"{total:,} active {tag.slug} RSS feeds",
        description=(
            f"{total:,} working RSS and Atom feeds tagged {tag.slug}, newest posts first."
            " Download them as OPML, or get any of them by email."
        ),
        canonical=f"{site_url}tag/{tag.slug}/",
        root="../../",
        generated=generated,
        data_url=data_url,
        tag=tag.slug,
        total=total,
        data=f"{data_url}tag/{tag.slug}/",
        opml_max=opml_max,
        links_max=links_max,
        rows=[_row(f, tag.slug, counts or {}, published, ignore) for f in tag.feeds[:PAGE_ROWS]],
    )


def render_index(
    tags: Sequence[Tag],
    listed: int,
    generated: datetime,
    success_days: int,
    item_years: int,
    total: int,
    *,
    site_url: str,
    data_url: str,
) -> str:
    topics = [(t.slug, len(t.feeds)) for t in tags]
    return _env.get_template("index.html").render(
        title="Active RSS feeds, by topic",
        description=(
            f"{total:,} working RSS and Atom feeds in {len(tags):,} topics, checked regularly."
            " Download them as OPML, or get any of them by email."
        ),
        canonical=site_url,
        root="",
        generated=generated,
        data_url=data_url,
        total=total,
        listed=listed,
        success_days=success_days,
        item_years=item_years,
        popular=topics[:INDEX_TAGS],
        topics=topics,  # all of them, for the filter box (tojson escapes </script>)
        filter_matches=FILTER_MATCHES,
        sources=SOURCES,
    )


def render_sitemap(tags: Iterable[Tag], generated: datetime, site_url: str) -> str:
    urls = [site_url] + [f"{site_url}tag/{t.slug}/" for t in tags]
    return _env.get_template("sitemap.xml").render(urls=urls, day=generated.date().isoformat())
