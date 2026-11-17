"""Publishing lists per tag and the website: selection, escaping of untrusted text, and the output
directory."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from defusedxml import ElementTree as SafeET

from indexer.export import (
    LINKS_MAX,
    MARKER,
    OPML_MAX,
    ExportError,
    ListedFeed,
    clean_output_dir,
    preview_path,
    render_links,
    render_opml,
    render_preview,
    top_tags,
    write_output,
)
from indexer.site import (
    FEED_TAGS,
    INDEX_TAGS,
    PAGE_ROWS,
    feed_tags,
    feedrule_href,
    render_tag_page,
    safe_href,
)

SITE = "https://s.example/feeds/"
DATA = "https://d.example/out/"


def page(tag: object) -> str:
    return render_tag_page(
        tag,  # type: ignore[arg-type]
        NOW,
        site_url=SITE,
        data_url=DATA,
        opml_max=OPML_MAX,
        links_max=LINKS_MAX,
    )


def rows(html: str) -> list[str]:
    return re.findall(r'<a class="mail" href="([^"]*)"', html)


def feed_links(html: str) -> list[str]:
    return re.findall(r'<a class="rss" href="([^"]*)"', html)


NOW = datetime(2026, 11, 16, tzinfo=UTC)


def feed(
    n: int, cats: str | None = None, items: str | None = None, *, day: int = 1, title: str = "T"
) -> ListedFeed:
    return ListedFeed(
        url=f"https://f{n}.example/feed",
        title=title,
        site_url=f"https://f{n}.example/",
        last_item_date=datetime(2026, 10, day, tzinfo=UTC),
        categories=cats,
        item_categories=items,
    )


def test_tags_come_from_both_category_columns() -> None:
    f = feed(1, "css, web dev", "css, javascript, web-dev")
    assert f.tags() == {"css", "web-dev", "javascript"}  # "web dev" and "web-dev" are one tag
    assert f.tags(frozenset({"css"})) == {"web-dev", "javascript"}


@pytest.mark.parametrize("bad", ["../etc", "a/b", "UPPER", "x" * 101, ".", "dot.ted"])
def test_unsafe_tag_names_never_become_directories(bad: str) -> None:
    assert feed(1, bad).tags() == set()


def test_top_tags_by_feed_count_then_name() -> None:
    feeds = [feed(1, "a, b, c"), feed(2, "a, b"), feed(3, "a, d"), feed(4, "uncategorized, d")]
    tags = top_tags(feeds, limit=3, ignore=["uncategorized"])
    assert [(t.slug, len(t.feeds)) for t in tags] == [("a", 3), ("b", 2), ("d", 2)]


def test_feeds_in_a_tag_newest_first() -> None:
    feeds = [feed(1, "x", day=3), feed(2, "x", day=9), feed(3, "x", day=9)]
    (tag,) = top_tags(feeds, 1)
    assert [f.url for f in tag.feeds] == [
        "https://f2.example/feed",
        "https://f3.example/feed",
        "https://f1.example/feed",
    ]


def test_only_http_links_are_written() -> None:
    # returned as is: the templates escape it
    assert safe_href(" https://a.example/x?a=1&b=2") == "https://a.example/x?a=1&b=2"
    for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "", None):
        assert safe_href(bad) is None


def test_tag_page_rows_are_escaped_and_capped() -> None:
    evil = '<script>alert(1)</script> & "x"'
    feeds = [feed(i, "x", title=evil) for i in range(PAGE_ROWS + 5)]
    feeds.append(
        ListedFeed("https://f0.example/feed", "ok", "javascript:alert(1)", None, "x", None)
    )
    (tag,) = top_tags(feeds, 1)
    html = page(tag)
    assert len(rows(html)) == PAGE_ROWS
    assert "<script>" not in html.split("<table")[1]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;x&#34;" in html  # autoescaped
    assert f"{PAGE_ROWS + 6} active feeds are tagged" in html
    # a non-http site URL is never a link: the feed's home page replaces it
    (only,) = top_tags([feeds[-1]], 1)
    assert "javascript:" not in page(only)
    assert '<a class="title" href="https://f0.example/" rel="nofollow">ok</a>' in page(only)


def test_feedrule_link_is_prefilled_and_encoded() -> None:
    assert (
        feedrule_href("https://github.com/rust-lang/rust/releases.atom")
        == "https://feedrule.com/?feed=https://github.com/rust-lang/rust/releases.atom"
    )
    assert feedrule_href('https://e.example/f?a=1&b="><script>|x').endswith(
        "feed=https://e.example/f%3Fa%3D1%26b%3D%22%3E%3Cscript%3E%7Cx"
    )
    (tag,) = top_tags([feed(1, "x")], 1)
    html = page(tag)
    assert rows(html) == ["https://feedrule.com/?feed=https://f1.example/feed"]
    assert '<img src="../../feedrule.svg" width="20" height="20" alt="Get by email">' in html
    # feed and site links are nofollow; feedrule.com links are not
    # the title and "Website" link to the website; "Feed" to the feed
    assert '<a class="title" href="https://f1.example/" rel="nofollow">T</a>' in html
    assert (
        'f1.example · <a class="site" href="https://f1.example/" rel="nofollow">Website</a>' in html
    )
    assert '<a class="rss" href="https://f1.example/feed" rel="nofollow">Feed</a>' in html
    assert 'href="https://feedrule.com/" rel' not in html


def test_feed_tags_most_widespread_first() -> None:
    f = feed(1, ", ".join(f"t{i}" for i in range(15)) + ", cur, uncategorized")
    counts = {f"t{i}": 100 - i for i in range(15)} | {"t14": 500}
    tags = feed_tags(f, "cur", counts, frozenset({"t14", "t0"}), frozenset({"uncategorized"}))
    shown = [t for t, _ in tags]
    assert len(shown) == FEED_TAGS and shown[:3] == ["t14", "t0", "t1"]
    assert "cur" not in shown and "uncategorized" not in shown  # the page's tag, ignored tags
    assert tags[:3] == [("t14", True), ("t0", True), ("t1", False)]  # linked if published
    assert feed_tags(feed(2, "cur"), "cur", {}, frozenset()) == []


def test_tag_pages_show_each_feeds_tags(tmp_path: Path) -> None:
    out = tmp_path / "out"
    feeds = [feed(1, "css, rust, rare"), feed(2, "css, rust"), feed(3, "rust")]
    write_output(out, feeds, top=2, ignore=[], generated=NOW, success_days=60, item_years=5)
    css = (out / "docs" / "tag" / "css" / "index.html").read_text()
    assert '<span class="ftags"><a href="../rust/">rust</a><span>rare</span></span>' in css


def test_opml_is_well_formed_and_escaped() -> None:
    evil = feed(1, "x", title='A & B "quoted" <tag>')
    (tag,) = top_tags([evil, feed(2, "x", title="")], 1)
    root = SafeET.fromstring(render_opml(tag, NOW).encode())
    outlines = root.findall("./body/outline")
    assert [o.get("xmlUrl") for o in outlines] == [
        "https://f1.example/feed",
        "https://f2.example/feed",
    ]
    assert outlines[0].get("title") == 'A & B "quoted" <tag>'
    assert outlines[1].get("title") == "https://f2.example/feed"  # no title: the URL
    assert outlines[0].get("htmlUrl") == "https://f1.example/"


def test_clean_output_dir_refuses_unrelated_directory(tmp_path: Path) -> None:
    (tmp_path / "precious.txt").write_text("keep me")
    with pytest.raises(ExportError, match="refusing"):
        clean_output_dir(tmp_path)
    assert (tmp_path / "precious.txt").exists()


def test_clean_output_dir_refuses_a_file(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.write_text("x")
    with pytest.raises(ExportError, match="not a directory"):
        clean_output_dir(target)


def test_clean_output_dir_replaces_previous_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    clean_output_dir(out)
    (out / "tag" / "old").mkdir(parents=True)
    (out / "tag" / "old" / "links.txt").write_text("stale")
    clean_output_dir(out)
    assert sorted(p.name for p in out.iterdir()) == [MARKER]


def test_write_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    feeds = [feed(1, "css", "web dev"), feed(2, "css"), feed(3, "rust")]
    tags = write_output(
        out,
        feeds,
        top=2,
        ignore=[],
        generated=NOW,
        success_days=60,
        item_years=5,
        site_url=SITE,
        data_url=DATA,
    )
    assert [t.slug for t in tags] == ["css", "rust"]
    css = out / "tag" / "css"
    assert sorted(p.name for p in css.iterdir()) == ["feeds.opml", "links.txt"]
    assert (css / "links.txt").read_text() == ("https://f1.example/feed\nhttps://f2.example/feed\n")
    assert not (out / "tag" / "web-dev").exists()  # not in the top 2
    assert not list(out.rglob("*.md"))  # no Markdown: the site is HTML
    docs = out / "docs"
    assert sorted(p.name for p in docs.iterdir()) == [
        ".nojekyll",
        "feedrule.svg",
        "index.html",
        "sitemap.xml",
        "style.css",
        "tag",
    ]
    assert (docs / "feedrule.svg").read_bytes().startswith(b"<svg ")
    index = (docs / "index.html").read_text()
    assert '<a href="tag/css/">css<span class="n">2</span></a>' in index
    assert "3 RSS and Atom feeds that work" in index
    assert f'<link rel="canonical" href="{SITE}">' in index
    tag_page = (docs / "tag" / "css" / "index.html").read_text()
    assert f'<a class="button" href="{DATA}tag/css/feeds.opml">' in tag_page
    assert f'href="{DATA}tag/css/links.txt"' in tag_page
    assert '<link rel="stylesheet" href="../../style.css">' in tag_page
    assert f'<link rel="canonical" href="{SITE}tag/css/">' in tag_page
    sitemap = SafeET.fromstring((docs / "sitemap.xml").read_bytes())
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    assert [u.findtext(f"{ns}loc") for u in sitemap] == [
        SITE,
        f"{SITE}tag/css/",
        f"{SITE}tag/rust/",
    ]


def test_urls_must_end_with_a_slash(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="end with"):
        write_output(
            tmp_path / "o",
            [],
            top=1,
            ignore=[],
            generated=NOW,
            success_days=1,
            item_years=1,
            site_url="https://s.example",
        )


def test_all_lists_every_feed_tagged_or_not(tmp_path: Path) -> None:
    out = tmp_path / "out"
    feeds = [feed(1, "css"), feed(2, ""), feed(3, "uncategorized"), feed(4, "rare")]
    write_output(
        out,
        feeds,
        top=1,
        ignore=["uncategorized"],
        generated=NOW,
        success_days=180,
        item_years=5,
        data_url=DATA,
    )
    urls = [f"https://f{i}.example/feed" for i in range(1, 5)]
    assert (out / "all" / "links.txt").read_text().splitlines() == urls
    root = SafeET.fromstring((out / "all" / "feeds.opml").read_bytes())
    assert root.findtext("./head/title") == "All active feeds"
    assert [o.get("xmlUrl") for o in root.findall("./body/outline")] == urls
    index = (out / "docs" / "index.html").read_text()
    assert "4 RSS and Atom feeds that work" in index
    assert "all/links.txt" not in index and "all/feeds.opml" not in index  # too big to offer
    assert "1 of\nthe feeds are in a topic" in index
    assert not (out / "tag" / "all").exists()  # all/ is outside tag/: no clash with a tag


def test_all_is_not_capped() -> None:
    (tag,) = top_tags([feed(i, "x") for i in range(OPML_MAX + 5)], 1)
    assert len(render_links(tag, None).splitlines()) == OPML_MAX + 5
    root = SafeET.fromstring(render_opml(tag, NOW, None).encode())
    assert len(root.findall("./body/outline")) == OPML_MAX + 5


def test_per_file_caps_keep_the_most_recently_active() -> None:
    # feed i posted on day (i % 28) + 1: the newest feeds have the highest day numbers
    feeds = [feed(i, "x", day=(i % 28) + 1) for i in range(LINKS_MAX + 300)]
    (tag,) = top_tags(feeds, 1)
    newest = tag.feeds  # newest post first

    links = render_links(tag).splitlines()
    assert len(links) == LINKS_MAX
    assert set(links) == {f.url for f in newest[:LINKS_MAX]}
    assert links == [f.url for f in newest[:LINKS_MAX]]  # newest post first

    root = SafeET.fromstring(render_opml(tag, NOW).encode())
    opml = [o.get("xmlUrl") for o in root.findall("./body/outline")]
    assert len(opml) == OPML_MAX
    assert set(opml) == {f.url for f in newest[:OPML_MAX]}

    html = page(tag)
    assert feed_links(html) == opml[:PAGE_ROWS]  # the page shows the start of feeds.opml
    assert f"{LINKS_MAX + 300:,} active feeds are tagged" in html
    assert f"Below: the {PAGE_ROWS} most recently active" in html
    assert f"has the {OPML_MAX} most recently active" in html
    assert f"lists the {LINKS_MAX:,} most recently active" in html


def test_small_tag_lists_everything() -> None:
    (tag,) = top_tags([feed(i, "x") for i in range(3)], 1)
    assert len(render_links(tag).splitlines()) == 3
    assert "Subscribe to all of them" in page(tag)


def test_tag_page_says_all_when_files_are_not_capped() -> None:
    (tag,) = top_tags([feed(i, "x") for i in range(PAGE_ROWS + 10)], 1)
    html = page(tag)
    assert f"has all {PAGE_ROWS + 10}; links.txt lists all {PAGE_ROWS + 10}," in html


def test_index_sorted_by_feed_count(tmp_path: Path) -> None:
    feeds = [feed(1, "b, a, c"), feed(2, "c, a"), feed(3, "c"), feed(4, "d")]
    write_output(
        tmp_path / "o", feeds, top=3, ignore=[], generated=NOW, success_days=180, item_years=5
    )
    # tag/tags.txt: the published tags, most feeds first, ties by name (b and d have 1: b wins)
    assert (tmp_path / "o" / "tag" / "tags.txt").read_text() == "c\na\nb\n"
    index = (tmp_path / "o" / "docs" / "index.html").read_text()
    assert re.findall(r'<li data-t="([^"]+)">', index) == ["c", "a", "b"]  # 3, 2, 1 feeds


def test_previews(tmp_path: Path) -> None:
    out = tmp_path / "out"
    long_url = "https://f9.example/feed?" + "q=x&" * 200  # hex of it would not fit a file name
    feeds = [
        ListedFeed(
            "https://f1.example/feed",
            '  A <b>"x"</b>\n',
            None,
            datetime(2026, 10, 3, tzinfo=UTC),
            "css, web dev",
            "uncategorized, rare",
            48213,
        ),
        ListedFeed(long_url, "", None, None, None, None),
    ]
    write_output(
        out, feeds, top=1, ignore=["uncategorized"], generated=NOW, success_days=180, item_years=5
    )
    digest = hashlib.sha256(b"https://f1.example/feed").hexdigest()
    assert preview_path("https://f1.example/feed") == f"previews/{digest[:2]}/{digest}.json"
    doc = json.loads((out / preview_path("https://f1.example/feed")).read_text(encoding="utf-8"))
    # every tag of the feed, published or not (only css is), minus the ignored ones
    assert doc == {
        "url": "https://f1.example/feed",
        "title": 'A <b>"x"</b>',  # plain text: the page must not render it as HTML
        "bytes": 48213,
        "last": "2026-10-03",
        "tags": ["css", "rare", "web-dev"],
    }
    other = json.loads((out / preview_path(long_url)).read_text(encoding="utf-8"))
    assert other == {"url": long_url, "title": None, "bytes": None, "last": None, "tags": []}
    assert len(list((out / "previews").rglob("*.json"))) == 2


def test_preview_is_indented() -> None:
    text = render_preview(feed(1, "css, rust", title="T"))
    assert text.startswith('{\n  "url": "https://f1.example/feed",\n') and text.endswith("}\n")
    assert '\n    "css",\n' in text


ROOT = Path(__file__).resolve().parents[1]


def test_spec_is_copied(tmp_path: Path) -> None:
    out, spec = tmp_path / "out", tmp_path / "SPEC.md"
    spec.write_text("# spec\n")
    write_output(
        out,
        [feed(1, "css")],
        top=1,
        ignore=[],
        generated=NOW,
        success_days=180,
        item_years=5,
        spec=spec,
    )
    assert (out / "SPEC.md").read_text() == "# spec\n"


def test_missing_spec_fails_before_touching_the_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(ExportError, match="not found"):
        write_output(
            out,
            [],
            top=1,
            ignore=[],
            generated=NOW,
            success_days=180,
            item_years=5,
            spec=tmp_path / "missing.md",
        )
    assert not out.exists()


def test_repository_spec_matches_the_output() -> None:
    """SPEC.md is hand-written: it must list every file pattern and the current limits."""
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    for pattern in (
        "tag/tags.txt",
        "tag/<tag>/links.txt",
        "tag/<tag>/feeds.opml",
        "all/links.txt",
        "all/feeds.opml",
        "previews/<h:2>/<h>.json",
        "docs/index.html",
        "docs/tag/<tag>/index.html",
        "docs/style.css",
        "docs/feedrule.svg",
        "docs/sitemap.xml",
        "docs/.nojekyll",
    ):
        assert f"`{pattern}`" in spec
    assert f"{LINKS_MAX:,}" in spec and f"the {OPML_MAX} most" in spec
    assert f"first {PAGE_ROWS} of those feeds" in spec and "SHA-256" in spec
    for field in ("url", "title", "bytes", "last", "tags"):
        assert f"`{field}`" in spec


def test_index_shows_the_largest_topics_and_searches_all(tmp_path: Path) -> None:
    feeds = [feed(i, f"t{i:03d}") for i in range(INDEX_TAGS + 20)] + [feed(999, "t000")]
    write_output(
        tmp_path / "o", feeds, top=1000, ignore=[], generated=NOW, success_days=180, item_years=5
    )
    index = (tmp_path / "o" / "docs" / "index.html").read_text()
    chips = re.findall(r'<li data-t="([^"]+)">', index)
    assert len(chips) == INDEX_TAGS and chips[0] == "t000"  # the largest first
    topics = json.loads(
        re.search(r'<script type="application/json" id="topics">(.*?)</script>', index).group(1)
    )  # type: ignore[union-attr]
    assert len(topics) == INDEX_TAGS + 20 and topics[0] == ["t000", 2]
    assert (tmp_path / "o" / "docs" / "tag" / "t069" / "index.html").exists()  # still published


def test_index_lists_the_sources_and_licenses(tmp_path: Path) -> None:
    out = tmp_path / "out"
    write_output(
        out, [feed(1, "css")], top=1, ignore=[], generated=NOW, success_days=180, item_years=5
    )
    index = (out / "docs" / "index.html").read_text()
    assert '<h2 id="sources">Where the feeds come from</h2>' in index
    assert (
        '<tr><td><a href="https://github.com/kagisearch/smallweb">kagisearch/smallweb</a></td>'
        "<td>MIT</td>" in index
    )
    assert 'href="data/' not in index  # links are absolute: the site is not the repository
