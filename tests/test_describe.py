"""Descriptive fields used for labelling: title, subtitle, categories, language, media type..."""

import pytest
from conftest import FIXTURES

from indexer.check.feed import MAX_CATEGORY_LIST_LEN, normalize_category, parse_feed

RSS_FULL = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:cc="http://backend.userland.com/creativeCommonsRssModule">
<channel>
  <title>  CSS &amp; More  </title>
  <description>&lt;p&gt;Notes on &lt;b&gt;CSS&lt;/b&gt;&lt;/p&gt;</description>
  <language>en_US</language>
  <category>web, CSS</category>
  <category>Accessibility</category>
  <category>css</category>
  <dc:subject>Browsers</dc:subject>
  <itunes:category text="Technology"><itunes:category text="Software"/></itunes:category>
  <itunes:category text="Education"/>
  <image><url>/logo.png</url></image>
  <copyright>(c) 2024 Someone</copyright>
  <cc:license>https://creativecommons.org/licenses/by/4.0/</cc:license>
  <item><title>Ep 1</title><enclosure url="https://cdn.example/1.mp3" type="audio/mpeg"/>
    <category>Node.js</category><category>c++</category></item>
  <item><title>Ep 2</title><enclosure url="https://cdn.example/2.m4a" type=""/></item>
  <item><title>Post</title><category>node.js, Web Dev</category>
    <dc:subject>Audio_Engineering</dc:subject></item>
</channel></rss>"""


def test_rss_description() -> None:
    info = parse_feed(RSS_FULL, "https://blog.example/feed")
    assert info.title == "CSS & More"
    assert info.subtitle == "Notes on CSS"
    assert info.language == "en-us"
    # feed level: split on commas, normalized, de-duplicated, sorted
    assert info.categories == "accessibility, browsers, css, web"
    # every item's categories: "Node.js" and "node.js" -> "nodejs", "c++" -> "cplusplus"
    assert info.item_categories == "audio_engineering, cplusplus, nodejs, web dev"
    assert info.itunes_category == "Education, Technology > Software"
    assert info.media_type == "podcast"  # 2 of 3 items carry audio (one by extension)
    assert info.logo == "https://blog.example/logo.png"
    assert info.rights == "(c) 2024 Someone"
    assert info.license == "https://creativecommons.org/licenses/by/4.0/"
    assert info.icon is None


def test_atom_description() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="de-DE">
      <title type="html">A &lt;em&gt;tiny&lt;/em&gt; blog</title>
      <subtitle type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Short <b>notes</b></div>
      </subtitle>
      <category term="rust"/><category term="WASM" label="WebAssembly"/><category label="x"/>
      <icon>/favicon.ico</icon><logo>https://a.example/logo.svg</logo>
      <rights>CC BY-SA</rights>
      <link rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/"/>
      <entry><title>t</title></entry>
    </feed>"""
    info = parse_feed(body, "https://a.example/atom.xml")
    assert info.title == "A tiny blog"
    assert info.subtitle == "Short notes"
    assert info.language == "de-de"
    assert info.categories == "rust, wasm, x"  # term preferred, label as fallback
    assert info.item_categories is None
    assert info.icon == "https://a.example/favicon.ico"
    assert info.logo == "https://a.example/logo.svg"
    assert info.rights == "CC BY-SA"
    assert info.license == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert info.media_type == "text"
    assert info.itunes_category is None


def test_youtube_feed_is_video() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:yt="http://www.youtube.com/xml/schemas/2015"
        xmlns:media="http://search.yahoo.com/mrss/">
      <title>Channel</title>
      <entry><yt:videoId>abc</yt:videoId></entry>
      <entry><media:group>
        <media:content url="https://x.example/v" type="application/x-shockwave-flash"
                       medium="video"/>
      </media:group></entry>
    </feed>"""
    assert parse_feed(body).media_type == "video"


def test_empty_feed_has_no_media_type() -> None:
    assert parse_feed(b"<rss><channel><title>t</title></channel></rss>").media_type is None


def test_rdf_description() -> None:
    info = parse_feed((FIXTURES / "rss10.xml").read_bytes())
    assert info.kind == "rdf"
    assert info.title  # the fixture has a channel title
    assert info.media_type == "text"


@pytest.mark.parametrize("lang", ["english", "e", "en us", "x" * 40, "12"])
def test_bad_language_dropped(lang: str) -> None:
    body = f"<rss><channel><language>{lang}</language></channel></rss>".encode()
    assert parse_feed(body).language is None


def test_long_and_dirty_values_are_capped_and_cleaned() -> None:
    title = "T\u200bitle\n\t" + "x" * 500
    cats = "".join(f"<category>c{i:03}</category>" for i in range(80))
    body = f"<rss><channel><title>{title}</title>{cats}<image><url>javascript:x</url></image>"
    body += "</channel></rss>"
    info = parse_feed(body.encode())
    assert info.title is not None and len(info.title) == 200 and info.title.startswith("T itle")
    assert info.categories is not None and len(info.categories.split(", ")) == 80
    assert info.logo is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C#", "csharp"),
        ("C++", "cplusplus"),
        ("F#", "fsharp"),
        ("Google+", "googleplus"),
        ("  Web\t  Dev ", "web dev"),
        ("a/b:c", "abc"),
        ("Snake_Case-and-Dash", "snake_case-and-dash"),
        ("Français", "francais"),  # accents folded to ASCII
        ("日本語", ""),  # no ASCII equivalent: dropped
        ("\uff37\uff45\uff42", "web"),  # full-width letters fold to ASCII
        ("!!!", ""),
    ],
)
def test_normalize_category(raw: str, expected: str) -> None:
    assert normalize_category(raw) == expected


def test_category_list_truncated_to_the_limit_whole_terms() -> None:
    cats = "".join(f"<category>term-{i:03}</category>" for i in range(400))
    items = f"<item>{cats}</item>"
    info = parse_feed(f"<rss><channel>{cats}{items}</channel></rss>".encode())
    for value in (info.categories, info.item_categories):
        assert value is not None and len(value) <= MAX_CATEGORY_LIST_LEN
        terms = value.split(", ")
        assert terms == sorted(terms) and all(len(t) == 8 for t in terms)  # no partial term
        assert len(value) > MAX_CATEGORY_LIST_LEN - 10


def test_single_overlong_category_is_cut() -> None:
    body = f"<rss><channel><category>{'x' * 3000}</category></channel></rss>"
    assert parse_feed(body.encode()).categories == "x" * MAX_CATEGORY_LIST_LEN


def test_categories_differing_only_in_case_or_symbols_merge() -> None:
    body = b"<rss><channel><category>CSS</category><category>css</category>"
    body += b"<category>C.S.S</category><category>C#</category><category>c sharp</category>"
    body += b"</channel></rss>"
    assert parse_feed(body).categories == "c sharp, csharp, css"


def test_categories_that_normalize_to_nothing_are_dropped() -> None:
    body = b"<rss><channel><category>!!!</category><category>, ,</category></channel></rss>"
    assert parse_feed(body).categories is None


def test_category_limit_is_2048() -> None:
    assert MAX_CATEGORY_LIST_LEN == 2048
