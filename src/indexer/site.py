"""The published website: static HTML pages in output/docs/, served by GitHub Pages from the
output branch's /docs folder. An index of the tags and one page per tag (its most recently
active feeds), a stylesheet, feedrule.com's icon, a sitemap and .nojekyll. The data files
(links.txt, OPML, previews) stay outside docs/ and are linked on raw.githubusercontent.com,
so they don't count toward the Pages size limit.

Every title, URL and date comes from untrusted feeds: text is HTML-escaped, and a link is only
written for an http(s) URL (a feed's site URL could be `javascript:`). Links to the feeds and
their sites are rel="nofollow"; links to feedrule.com are not.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from html import escape, unescape
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape as xml_escape

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

# The source lists and their licenses, shown on the index. Update with data/sources.toml.
# (name, url, license, license url or "", what it has, "how" link or "")
SOURCES: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("kagisearch/smallweb", "https://github.com/kagisearch/smallweb", "MIT", "",
     "~44,000 personal sites", ""),
    ("outcoldman/hackernews-personal-blogs",
     "https://github.com/outcoldman/hackernews-personal-blogs", "MIT", "",
     "~1,230 Hacker News users' blogs", ""),
    ("simevidas/web-dev-feeds", "https://github.com/simevidas/web-dev-feeds", "MIT", "",
     "~1,070 web development blogs", ""),
    ("plenaryapp/awesome-rss-feeds", "https://github.com/plenaryapp/awesome-rss-feeds",
     "CC0-1.0", "", "~750 by topic, and news sites by country", ""),
    ("tuan3w/awesome-tech-rss", "https://github.com/tuan3w/awesome-tech-rss", "CC0-1.0", "",
     "~140 startups, science, technology", ""),
    ("matthiasjost/dotnet-creators-opml", "https://github.com/matthiasjost/dotnet-creators-opml",
     "MIT", "", "~140 .NET blogs", ""),
    ("data/survivors.txt", f"{REPO}/blob/develop/data/survivors.txt", "Common Crawl terms",
     "https://commoncrawl.org/terms-of-use", "old feeds still active today, reviewed by hand",
     f"{REPO}#feeds-from-common-crawl"),
    ("data/submissions.txt", f"{REPO}/blob/develop/data/submissions.txt", "MIT", "",
     "feeds submitted to the project", ""),
)  # fmt: skip


def safe_href(url: str | None) -> str | None:
    """An untrusted URL usable as an href (escaped), or None unless it is http(s)."""
    if not url or urlsplit(url.strip()).scheme.lower() not in ("http", "https"):
        return None
    return escape(url.strip(), quote=True)


def text(value: str) -> str:
    """Untrusted text for an HTML element or attribute: whitespace collapsed, escaped."""
    return escape(" ".join(value.split()), quote=True)


def feedrule_link(url: str, icon: str) -> str:
    """feedrule.com's icon linking to its form prefilled with the feed `url`, which is
    percent-encoded (only ':' and '/' kept) so it stays one query value."""
    href = escape(FEEDRULE_FORM + quote(url, safe=":/"), quote=True)
    return (
        f'<a class="mail" href="{href}" title="Get this feed by email (feedrule.com)">'
        f'<img src="{icon}" width="20" height="20" alt="Get by email"></a>'
    )


def feed_tags(
    feed: ListedFeed,
    current: str,
    counts: Mapping[str, int],
    published: frozenset[str],
    ignore: frozenset[str] = frozenset(),
) -> str:
    """Up to FEED_TAGS of the feed's tags other than `current`, the most widespread first (by
    number of listed feeds: tag frequency inside a feed is not stored); published tags link to
    their page (a sibling of the current tag's page)."""
    slugs = sorted(feed.tags(ignore) - {current}, key=lambda t: (-counts.get(t, 0), t))
    if not slugs:
        return ""
    chips = [
        f'<a href="../{escape(t)}/">{escape(t)}</a>'
        if t in published
        else f"<span>{escape(t)}</span>"
        for t in slugs[:FEED_TAGS]
    ]
    return f'<span class="ftags">{"".join(chips)}</span>'


def _date(value: datetime | None) -> str:
    if value is None:
        return ""
    day = value.date().isoformat()
    return f'<time datetime="{day}">{day}</time>'


def _page(
    *,
    title: str,
    description: str,
    canonical: str,
    root: str,
    body: str,
    generated: datetime,
    data_url: str,
) -> str:
    """A whole page. `root` is the relative path to docs/ ("" or "../../")."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{canonical}">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="{root}{ICON}" type="image/svg+xml">
<link rel="stylesheet" href="{root}{STYLE}">
</head>
<body>
<header class="bar"><div class="wrap">
<a class="brand" href="{root or "./"}"><img src="{root}{ICON}" width="24" height="24" alt="">Active RSS feeds</a>
<a class="cta" href="{FEEDRULE}">Get any feed by email</a>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="wrap">
<p>Updated {generated.date().isoformat()}. Every feed is fetched and checked regularly; the lists
are rebuilt after each check run. <a href="{REPO}">How it works and source code</a> ·
<a href="{escape(data_url, quote=True)}SPEC.md">Data files</a> ·
Email digests by <a href="{FEEDRULE}">feedrule.com</a>.</p>
</footer>
</body>
</html>
"""  # noqa: E501 - HTML


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
    counts = counts or {}
    total = len(tag.feeds)
    shown = tag.feeds[:PAGE_ROWS]
    name = escape(tag.slug)
    data = escape(f"{data_url}tag/{tag.slug}/", quote=True)

    def some(cap: int) -> str:
        return f"all {total:,}" if total <= cap else f"the {cap:,} most recently active"

    if total <= PAGE_ROWS:
        intro = (
            f"{total:,} active feeds tagged <b>{name}</b>, newest post first. Subscribe to all"
            " of them at once by importing the OPML file into a feed reader."
        )
    else:
        intro = (
            f"{total:,} active feeds are tagged <b>{name}</b>. Below: the {len(shown):,} most"
            f" recently active, newest post first. The OPML file (to import into a feed reader)"
            f" has {some(opml_max)}; links.txt lists {some(links_max)}, one feed URL per line."
        )
    rows = []
    for feed in shown:
        label = text(feed.title or "") or text(feed.url)
        href = safe_href(feed.url) or ""
        # the feed's website, or else the home page of the feed's own domain
        parts = urlsplit(feed.url)
        site = safe_href(feed.site_url) or safe_href(f"{parts.scheme}://{parts.netloc}/") or href
        host = text(urlsplit(unescape(site)).hostname or "")
        rows.append(
            f"<tr><td>{feedrule_link(feed.url, f'../../{ICON}')}</td>"
            f'<td class="feed"><a class="title" href="{site}" rel="nofollow">{label}</a>'
            f'<span class="meta">{host} · '
            f'<a class="site" href="{site}" rel="nofollow">Website</a> · '
            f'<a class="rss" href="{href}" rel="nofollow">Feed</a></span>'
            f"{feed_tags(feed, tag.slug, counts, published, ignore)}</td>"
            f'<td class="date">{_date(feed.last_item_date)}</td></tr>'
        )
    body = f"""<nav class="crumbs"><a href="../../">All topics</a> / {name}</nav>
<h1>{name} <span class="muted">RSS feeds</span></h1>
<p class="lead">{intro}</p>
<p class="actions"><a class="button" href="{data}feeds.opml">Download OPML</a>
<a class="button ghost" href="{data}links.txt">links.txt</a></p>
<p class="tip"><img src="../../{ICON}" width="20" height="20" alt=""><span>Click the icon next
to a feed to get its new posts by email: a free digest from <a href="{FEEDRULE}">feedrule.com</a>,
with keyword filters and no account.</span></p>
<table class="feeds">
<thead><tr><th><span class="sr">Email</span></th><th>Feed</th><th class="date">Last post</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>"""  # noqa: E501 - HTML
    return _page(
        title=f"{total:,} active {name} RSS feeds",
        description=(
            f"{total:,} working RSS and Atom feeds tagged {name}, newest posts first."
            " Download them as OPML, or get any of them by email."
        ),
        canonical=escape(f"{site_url}tag/{tag.slug}/", quote=True),
        root="../../",
        body=body,
        generated=generated,
        data_url=data_url,
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
    chips = "\n".join(
        f'<li data-t="{escape(t.slug)}"><a href="tag/{escape(t.slug)}/">{escape(t.slug)}'
        f'<span class="n">{len(t.feeds):,}</span></a></li>'
        for t in tags[:INDEX_TAGS]
    )
    # every topic for the filter box; slugs are [a-z0-9_-], so the JSON cannot end the script
    every = json.dumps([[t.slug, len(t.feeds)] for t in tags], separators=(",", ":"))
    sources = "\n".join(
        f'<tr><td><a href="{url}">{escape(name)}</a></td><td>'
        + (f'<a href="{lic_url}">{escape(lic)}</a>' if lic_url else escape(lic))
        + f"</td><td>{escape(what)}"
        + (f' (<a href="{how}">how</a>)' if how else "")
        + "</td></tr>"
        for name, url, lic, lic_url, what, how in SOURCES
    )
    body = f"""<h1>Active RSS feeds, by topic</h1>
<p class="lead">{total:,} RSS and Atom feeds that work: each was fetched and parsed successfully
in the last {success_days} days and has a post from the last {item_years} years.</p>
<p class="tip"><img src="{ICON}" width="20" height="20" alt=""><span>Follow any of them by email:
click the icon next to a feed for a free digest from <a href="{FEEDRULE}">feedrule.com</a>, with
keyword filters and no account.</span></p>
<h2>Popular topics</h2>
<p class="muted">The {min(len(tags), INDEX_TAGS):,} largest of {len(tags):,} topics. {listed:,} of
the feeds are in a topic; the rest publish no categories, or only rare ones.</p>
<input id="q" class="filter" type="search" placeholder="Search all {len(tags):,} topics" aria-label="Search topics" hidden>
<ul id="tags" class="tags">
{chips}
</ul>
<ul id="found" class="tags" hidden></ul>
<script type="application/json" id="topics">{every}</script>
<h2 id="sources">Where the feeds come from</h2>
<div class="scroll"><table class="sources">
<thead><tr><th>Source</th><th>License</th><th>Feeds</th></tr></thead>
<tbody>
{sources}
</tbody>
</table></div>
<script>
const q = document.getElementById("q"), popular = document.getElementById("tags"),
  found = document.getElementById("found"),
  topics = JSON.parse(document.getElementById("topics").textContent);
q.hidden = false;
q.addEventListener("input", () => {{
  const v = q.value.trim().toLowerCase().split(/\\s+/).join("-");
  popular.hidden = v !== "";
  found.hidden = v === "";
  found.replaceChildren();
  if (!v) return;
  const hits = topics.filter(([t]) => t.includes(v)).slice(0, {FILTER_MATCHES});
  for (const [t, n] of hits) {{
    const li = document.createElement("li"), a = document.createElement("a"),
      count = document.createElement("span");
    a.href = `tag/${{t}}/`;
    a.textContent = t;
    count.className = "n";
    count.textContent = n.toLocaleString("en");
    a.append(count);
    li.append(a);
    found.append(li);
  }}
  if (!hits.length) found.textContent = "No topic matches.";
}});
</script>"""  # noqa: E501 - HTML
    return _page(
        title="Active RSS feeds, by topic",
        description=(
            f"{total:,} working RSS and Atom feeds in {len(tags):,} topics, checked regularly."
            " Download them as OPML, or get any of them by email."
        ),
        canonical=escape(site_url, quote=True),
        root="",
        body=body,
        generated=generated,
        data_url=data_url,
    )


def render_sitemap(tags: Iterable[Tag], generated: datetime, site_url: str) -> str:
    day = generated.date().isoformat()
    urls = [site_url] + [f"{site_url}tag/{t.slug}/" for t in tags]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for url in urls:
        lines.append(f"  <url><loc>{xml_escape(url)}</loc><lastmod>{day}</lastmod></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"
