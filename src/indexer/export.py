"""Write the published output: data files at the top (output/tag/<tag>/{links.txt,feeds.opml},
the tag names in output/tag/tags.txt (most feeds first), every listed feed in
output/all/{links.txt,feeds.opml} (tagged or not), one small JSON preview per listed feed in
output/previews/ (for a search page, see `preview_path` and `render_preview`), a copy of the
repository's SPEC.md), and the website in output/docs/ (see `indexer.site`).

A feed is listed when it had a successful check in the last `success_days` days and its newest
item (as of its latest successful check) is less than `item_years` years old. Its tags are its
feed-level and item categories (already normalized to lower-case ASCII, `a-z0-9-_` and spaces).
Only the `top_tags` tags with the most listed feeds get a directory; output/all has every listed
feed, including the many that publish no categories.

Everything here comes from untrusted feeds, so text is escaped for HTML and XML, and tag
names are checked again before they become directory names. The output directory is cleared
before writing, but only if it is empty or was created by this exporter (it holds a marker
file), so a mistyped path cannot wipe an unrelated directory.
"""

import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from importlib import resources
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from indexer import site

MARKER = ".indexer-output"
SLUG = re.compile(r"[a-z0-9_-]{1,100}")
# Per-tag caps. Each file keeps the most recently active feeds (newest post first), so the
# tag page (site.PAGE_ROWS) lists the first feeds of feeds.opml, which lists the first of links.txt.
LINKS_MAX = 50_000  # links.txt (~35 bytes a line: ~1.7 MB at most)
OPML_MAX = 500  # feeds.opml
# Where the output branch is served: docs/ by GitHub Pages, the data files from raw.
SITE_URL = "https://feedrule.github.io/indexer/"
DATA_URL = "https://raw.githubusercontent.com/feedrule/indexer/output/"


@dataclass(frozen=True, slots=True)
class ListedFeed:
    url: str
    title: str | None
    site_url: str | None
    last_item_date: datetime | None
    categories: str | None
    item_categories: str | None
    file_size: int | None = None  # body bytes at the latest successful check

    def tags(self, ignore: frozenset[str] = frozenset()) -> set[str]:
        """Directory names for this feed's tags (spaces become dashes)."""
        out: set[str] = set()
        for field in (self.categories, self.item_categories):
            for tag in (field or "").split(","):
                slug = "-".join(tag.split())
                if slug and slug not in ignore and SLUG.fullmatch(slug):
                    out.add(slug)
        return out


@dataclass(frozen=True, slots=True)
class Tag:
    slug: str
    feeds: tuple[ListedFeed, ...]  # newest item first, then by URL


def top_tags(feeds: Iterable[ListedFeed], limit: int, ignore: Iterable[str] = ()) -> list[Tag]:
    """The `limit` tags with the most feeds (ties broken by name), each with its feeds."""
    ignored = frozenset("-".join(t.split()) for t in ignore)
    members: defaultdict[str, list[ListedFeed]] = defaultdict(list)
    for feed in feeds:
        for slug in feed.tags(ignored):
            members[slug].append(feed)
    ranked = sorted(members.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]
    return [Tag(slug, tuple(sorted(fs, key=_newest_first))) for slug, fs in ranked]


def _newest_first(feed: ListedFeed) -> tuple[float, str]:
    ts = feed.last_item_date.timestamp() if feed.last_item_date else float("-inf")
    return (-ts, feed.url)


# --- rendering --------------------------------------------------------------------------------


def by_popularity(tags: Iterable[Tag]) -> list[Tag]:
    """Most feeds first; ties by name."""
    return sorted(tags, key=lambda t: (-len(t.feeds), t.slug))


def render_tag_names(tags: Iterable[Tag]) -> str:
    """tag/tags.txt: one published tag per line, most feeds first. (Slugs have no dot, so the
    file cannot clash with a tag directory.)"""
    return "".join(f"{tag.slug}\n" for tag in by_popularity(tags))


def render_links(tag: Tag, limit: int | None = LINKS_MAX) -> str:
    """The `limit` most recently active feeds (all if None), newest post first: the first lines
    are the best results, so a search page can show them in file order."""
    return "".join(f"{f.url}\n" for f in tag.feeds[:limit])


def render_opml(
    tag: Tag, generated: datetime, limit: int | None = OPML_MAX, title: str | None = None
) -> str:
    """The `limit` most recently active feeds (all if None), sorted by URL."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{xml_escape(title or f'{tag.slug} feeds')}</title>",
        f"    <dateCreated>{format_datetime(generated)}</dateCreated>",
        "  </head>",
        "  <body>",
    ]
    for feed in sorted(tag.feeds[:limit], key=lambda f: f.url):  # most active, by URL
        title = quoteattr(" ".join((feed.title or feed.url).split()))
        attrs = f'type="rss" text={title} title={title} xmlUrl={quoteattr(feed.url)}'
        if feed.site_url:
            attrs += f" htmlUrl={quoteattr(feed.site_url)}"
        lines.append(f"    <outline {attrs}/>")
    lines += ["  </body>", "</opml>"]
    return "\n".join(lines) + "\n"


def preview_path(url: str) -> str:
    """previews/<2 hex>/<sha256 of the URL, hex>.json: fixed length whatever the URL (hex of the
    URL itself can exceed the 255-byte file name limit), split in 256 folders. A browser computes
    it with crypto.subtle.digest("SHA-256", new TextEncoder().encode(url))."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"previews/{digest[:2]}/{digest}.json"


def render_preview(feed: ListedFeed, ignore: frozenset[str] = frozenset()) -> str:
    """One feed's preview, for a search page showing the feeds of a tag's links.txt:

        {"url": "https://...", "title": "...", "bytes": 48213, "last": "2026-11-14",
         "tags": ["rust", "web-dev"]}

    url is the feed URL the file is named after, so a preview stands alone. title, bytes (body
    size at the latest successful check) and last are null when unknown; tags are all the feed's
    tags (sorted; only those in tag/tags.txt have a folder). The title is untrusted plain text."""
    doc = {
        "url": feed.url,
        "title": " ".join(feed.title.split()) or None if feed.title else None,
        "bytes": feed.file_size,
        "last": feed.last_item_date.date().isoformat() if feed.last_item_date else None,
        "tags": sorted(feed.tags(ignore)),
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


# --- writing ----------------------------------------------------------------------------------


class ExportError(Exception):
    pass


def clean_output_dir(path: Path) -> None:
    """Empty `path` (creating it if needed). Refuses a non-empty directory without the marker."""
    if path.exists():
        if not path.is_dir():
            raise ExportError(f"{path} exists and is not a directory")
        entries = list(path.iterdir())
        if entries and not (path / MARKER).is_file():
            raise ExportError(
                f"{path} is not empty and was not created by indexer export ({MARKER} missing);"
                " refusing to delete it"
            )
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    path.mkdir(parents=True, exist_ok=True)
    (path / MARKER).write_text("Generated by `indexer export`; this directory is replaced.\n")


def write_output(
    path: Path,
    feeds: Sequence[ListedFeed],
    *,
    top: int,
    ignore: Iterable[str],
    generated: datetime,
    success_days: int,
    item_years: int,
    spec: Path | None = None,
    site_url: str = SITE_URL,
    data_url: str = DATA_URL,
) -> list[Tag]:
    """Write everything to `path` (cleared first), with a copy of `spec` (SPEC.md) if given:
    the data files at the top, the website in docs/. `site_url` and `data_url` end with '/'."""
    if spec is not None and not spec.is_file():
        raise ExportError(f"{spec} not found: the output's SPEC.md is copied from it")
    if not (site_url.endswith("/") and data_url.endswith("/")):
        raise ExportError("--site-url and --data-url must end with '/'")
    ignored = frozenset("-".join(t.split()) for t in ignore)
    tags = top_tags(feeds, top, ignored)
    clean_output_dir(path)
    root = path.resolve()
    unique = {f.url: f for f in feeds}.values()
    counts = Counter(slug for f in unique for slug in f.tags(ignored))  # listed feeds per tag
    published = frozenset(t.slug for t in tags)
    docs = path / "docs"
    for tag in tags:
        directory = path / "tag" / tag.slug
        page = docs / "tag" / tag.slug
        if not SLUG.fullmatch(tag.slug) or not all(
            d.resolve().is_relative_to(root) for d in (directory, page)
        ):
            raise ExportError(f"unsafe tag directory {tag.slug!r}")  # cannot happen
        directory.mkdir(parents=True)
        page.mkdir(parents=True)
        (directory / "links.txt").write_text(render_links(tag), encoding="utf-8")
        (directory / "feeds.opml").write_text(render_opml(tag, generated), encoding="utf-8")
        (page / "index.html").write_text(
            site.render_tag_page(
                tag,
                generated,
                site_url=site_url,
                data_url=data_url,
                opml_max=OPML_MAX,
                links_max=LINKS_MAX,
                counts=counts,
                published=published,
                ignore=ignored,
            ),
            encoding="utf-8",
        )
    (path / "tag").mkdir(exist_ok=True)
    (path / "tag" / "tags.txt").write_text(render_tag_names(tags), encoding="utf-8")
    everything = Tag("all", tuple(sorted(unique, key=_newest_first)))
    (path / "all").mkdir()
    (path / "all" / "links.txt").write_text(render_links(everything, None), encoding="utf-8")
    (path / "all" / "feeds.opml").write_text(
        render_opml(everything, generated, None, "All active feeds"), encoding="utf-8"
    )
    for feed in everything.feeds:
        preview = path / preview_path(feed.url)
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(render_preview(feed, ignored), encoding="utf-8")
    if spec is not None:
        shutil.copyfile(spec, path / "SPEC.md")
    listed = len({f.url for t in tags for f in t.feeds})
    docs.mkdir(exist_ok=True)
    package = resources.files("indexer")
    for name in (site.ICON, site.STYLE):
        (docs / name).write_bytes(package.joinpath(name).read_bytes())
    (docs / ".nojekyll").write_text("")
    ranked = by_popularity(tags)
    (docs / "index.html").write_text(
        site.render_index(
            ranked,
            listed,
            generated,
            success_days,
            item_years,
            len(everything.feeds),
            site_url=site_url,
            data_url=data_url,
        ),
        encoding="utf-8",
    )
    (docs / "sitemap.xml").write_text(
        site.render_sitemap(ranked, generated, site_url), encoding="utf-8"
    )
    return tags
