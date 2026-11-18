from datetime import UTC, datetime

import pytest
from conftest import FIXTURES

from indexer.check.feed import clean_url, parse_date, parse_feed
from indexer.errors import CheckError, ErrorCode


def parse(name: str):
    return parse_feed((FIXTURES / name).read_bytes())


def error_of(body: bytes) -> ErrorCode:
    with pytest.raises(CheckError) as info:
        parse_feed(body)
    return info.value.code


def test_rss2() -> None:
    info = parse("rss2.xml")
    assert info.kind == "rss"
    assert info.item_count == 3
    assert info.ttl == 60
    assert info.last_item_date == datetime(2024, 3, 5, 8, 30, tzinfo=UTC)


def test_atom() -> None:
    info = parse("atom.xml")
    assert info.kind == "atom"
    assert info.item_count == 2
    assert info.ttl is None
    assert info.last_item_date == datetime(2025, 5, 20, tzinfo=UTC)


def test_rss10_rdf() -> None:
    info = parse("rss10.xml")
    assert (info.kind, info.item_count) == ("rdf", 2)
    assert info.last_item_date == datetime(2019, 4, 2, 10, tzinfo=UTC)


def test_rss091_with_external_doctype_is_parsed_without_loading_it() -> None:
    info = parse("rss091_doctype.xml")
    assert (info.kind, info.item_count, info.last_item_date) == ("rss", 1, None)


def test_billion_laughs_refused() -> None:
    assert error_of((FIXTURES / "billion_laughs.xml").read_bytes()) is ErrorCode.PARSE_ERROR


def test_external_entity_refused() -> None:
    assert error_of((FIXTURES / "xxe.xml").read_bytes()) is ErrorCode.PARSE_ERROR


def test_html_is_not_a_feed() -> None:
    assert error_of((FIXTURES / "page.html").read_bytes()) is ErrorCode.NOT_A_FEED


def test_other_xml_is_not_a_feed() -> None:
    assert error_of((FIXTURES / "sitemap.xml").read_bytes()) is ErrorCode.NOT_A_FEED


@pytest.mark.parametrize("body", [b'{"version": "https://jsonfeed.org/version/1"}', b"", b"hello"])
def test_non_markup_is_not_a_feed(body: bytes) -> None:
    assert error_of(body) is ErrorCode.NOT_A_FEED


def test_broken_xml_is_parse_error() -> None:
    assert error_of((FIXTURES / "broken.xml").read_bytes()) is ErrorCode.PARSE_ERROR


def test_rss_without_channel() -> None:
    assert error_of(b"<rss version='2.0'/>") is ErrorCode.NOT_A_FEED


@pytest.mark.parametrize("ttl", ["abc", "-5", "", "99999999999"])
def test_bad_ttl_ignored(ttl: str) -> None:
    body = f"<rss><channel><ttl>{ttl}</ttl></channel></rss>".encode()
    assert parse_feed(body).ttl is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mon, 01 Jan 2024 10:00:00 +0000", datetime(2024, 1, 1, 10, tzinfo=UTC)),
        ("Mon, 01 Jan 2024 10:00:00 -0000", datetime(2024, 1, 1, 10, tzinfo=UTC)),
        ("2024-01-01T10:00:00Z", datetime(2024, 1, 1, 10, tzinfo=UTC)),
        ("2024-01-01", datetime(2024, 1, 1, tzinfo=UTC)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(text: str | None, expected: datetime | None) -> None:
    assert parse_date(text) == expected


def test_rss_hints() -> None:
    body = b"""<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"
        xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
      <link>https://example.com/</link>
      <atom:link rel="self" href="/feed.xml" type="application/rss+xml"/>
      <itunes:new-feed-url>https://new.example/podcast.xml</itunes:new-feed-url>
      <generator>  WordPress 6.4
      </generator>
    </channel></rss>"""
    info = parse_feed(body, "https://old.example/blog/rss")
    assert info.kind == "rss"
    assert info.site_url == "https://example.com/"
    assert info.self_url == "https://old.example/feed.xml"  # relative, resolved
    assert info.new_feed_url == "https://new.example/podcast.xml"
    assert info.generator == "WordPress 6.4"


def test_atom_hints() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <link href="https://example.com/atom.xml" rel="self"/>
      <link href="https://example.com/"/>
      <generator uri="https://jekyllrb.com/" version="4.3.2">Jekyll</generator>
    </feed>"""
    info = parse_feed(body)
    assert (info.site_url, info.self_url) == (
        "https://example.com/",
        "https://example.com/atom.xml",
    )
    assert info.generator == "Jekyll 4.3.2"
    assert info.new_feed_url is None


def test_rdf_generator_and_link() -> None:
    body = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/" xmlns:admin="http://webns.net/mvcb/">
      <channel><link>http://example.org/</link>
        <admin:generatorAgent rdf:resource="http://www.movabletype.org/?v=3.2"/></channel>
    </rdf:RDF>"""
    info = parse_feed(body)
    assert info.site_url == "http://example.org/"
    assert info.generator == "http://www.movabletype.org/?v=3.2"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("javascript:alert(1)", None),
        ("file:///etc/passwd", None),
        ("https://a.example/\nx", None),
        ("https://a.example/\x00", None),
        ("https://a.example/" + "a" * 3000, None),
        ("  https://a.example/  ", "https://a.example/"),
        ("", None),
    ],
)
def test_clean_url(value: str, expected: str | None) -> None:
    assert clean_url(value) == expected


@pytest.mark.parametrize("encoding", ["Shift_JIS", "EUC-JP", "GB2312", "Big5", "EUC-KR"])
def test_multi_byte_declared_encodings_parse(encoding: str) -> None:
    """Expat refuses these encodings ("multi-byte encodings are not supported"); the body is
    re-encoded to UTF-8 with Python's codecs and parsed again."""
    title = {"Big5": "繁體中文", "GB2312": "简体中文", "EUC-KR": "한국어"}.get(
        encoding, "日本語のブログ"
    )
    xml = (
        f'<?xml version="1.0" encoding="{encoding}"?>\n'
        f"<rss><channel><title>{title}</title>"
        "<item><pubDate>Mon, 09 Nov 2026 10:00:00 GMT</pubDate><category>news</category></item>"
        "</channel></rss>"
    )
    info = parse_feed(xml.encode(encoding))
    assert info.title == title
    assert info.last_item_date == datetime(2026, 11, 9, 10, tzinfo=UTC)


def test_unknown_declared_encoding_is_a_parse_error() -> None:
    body = b'<?xml version="1.0" encoding="x-no-such-charset"?><rss><channel/></rss>'
    assert error_of(body) == ErrorCode.PARSE_ERROR


def test_reencoding_keeps_the_xml_protections() -> None:
    bomb = (
        '<?xml version="1.0" encoding="Shift_JIS"?>'
        '<!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss>&b;</rss>'
    ).encode("shift_jis")
    assert error_of(bomb) == ErrorCode.PARSE_ERROR


def test_item_links_as_written_distinct_in_order() -> None:
    rss = b"""<rss version="2.0"><channel><title>t</title>
      <item><link> https://a.example/1 </link></item>
      <item><link>/relative/2</link></item>
      <item><link>https://a.example/1</link></item>
      <item><title>no link</title></item>
      <item><link>javascript:alert(1)</link></item>
    </channel></rss>"""
    assert parse_feed(rss).item_links == (
        "https://a.example/1",
        "/relative/2",  # as written: not resolved
        "javascript:alert(1)",  # not validated: shown to reviewers, never fetched
    )
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><link rel="enclosure" href="https://a.example/a.mp3"/>
             <link href="https://a.example/post"/></entry>
      <entry><link rel="alternate" href="https://spam.example/x"/></entry>
    </feed>"""
    assert parse_feed(atom).item_links == ("https://a.example/post", "https://spam.example/x")
    rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/"><channel><title>t</title></channel>
      <item><link>https://a.example/r</link></item></rdf:RDF>"""
    assert parse_feed(rdf).item_links == ("https://a.example/r",)


def test_item_links_are_the_three_most_recent_items() -> None:
    rss = b"""<rss version="2.0"><channel><title>t</title>
      <item><link>https://a.example/old</link>
        <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
      <item><link>https://a.example/undated</link></item>
      <item><link>https://a.example/newest</link>
        <pubDate>Sun, 01 Sep 2024 00:00:00 GMT</pubDate></item>
      <item><link>https://a.example/mid</link>
        <pubDate>Mon, 01 Apr 2024 00:00:00 GMT</pubDate></item>
      <item><link>https://a.example/new</link>
        <pubDate>Mon, 01 Jul 2024 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    assert parse_feed(rss).item_links == (
        "https://a.example/newest",
        "https://a.example/new",
        "https://a.example/mid",
    )
    undated = b"""<rss version="2.0"><channel><title>t</title>
      <item><link>https://a.example/1</link></item><item><link>https://a.example/2</link></item>
    </channel></rss>"""
    assert parse_feed(undated).item_links == ("https://a.example/1", "https://a.example/2")


def test_item_links_are_capped() -> None:
    items = "".join(
        f"<item><link>https://a.example/{i}/{'x' * 400}</link></item>" for i in range(150)
    )
    links = parse_feed(f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()).item_links
    assert len(links) == 3 and all(len(link) == 300 for link in links)
