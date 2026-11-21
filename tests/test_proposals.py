"""Rendering survivors' proposals: data/survivors.txt entries and the pull request report."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from defusedxml import ElementTree as SafeET

from indexer.check.feed import RecentPost
from indexer.sources import extract_urls
from indexer.survivors.proposals import (
    _MAX_REPORT_CHARS,
    _one_line,
    _render_opml,
    acceptance,
    known_urls,
    log_line,
    render_entries,
    render_report,
)
from indexer.survivors.run import Survivor

NOW = datetime(2026, 11, 19, tzinfo=UTC)
WAT = "https://data.commoncrawl.org/crawl-data/CC-MAIN-2021-49/segments/1/wat/x.warc.wat.gz"


def survivor(url: str, title: str | None, language: str | None = "en") -> Survivor:
    return Survivor(url, WAT, title, language, datetime(2026, 11, 12, 9, tzinfo=UTC))


def test_entries_are_comment_lines_and_the_url_commented_out() -> None:
    posts = (
        RecentPost("https://missfrugalmommy.com/ten-ways/", "Ten ways to save", "We spend..."),
        RecentPost("https://missfrugalmommy.com/b/", None, None),
    )
    frugal = survivor("https://missfrugalmommy.com/feed/", "Miss Frugal Mommy")
    block = render_entries(
        [replace(frugal, subtitle="Saving money with kids", recent_posts=posts)],
        "CC-MAIN-2021-49",
        date(2026, 11, 18),
    )
    assert block == (
        "\n# --- 2026-11-18, from CC-MAIN-2021-49 ---\n"
        "\n# Miss Frugal Mommy · en · last post 2026-11-12\n"
        "# about: Saving money with kids\n"
        f"# found in {WAT}\n"
        "# recent posts:\n"
        "#   - https://missfrugalmommy.com/ten-ways/\n"
        "#     Ten ways to save\n"
        "#     We spend...\n"
        "#   - https://missfrugalmommy.com/b/\n"
        "# https://missfrugalmommy.com/feed/\n"
    )
    assert extract_urls(block.encode(), "lines") == []  # nothing loaded until a reviewer accepts
    accepted = block.replace(
        "# https://missfrugalmommy.com/feed/", "https://missfrugalmommy.com/feed/"
    )
    assert extract_urls(accepted.encode(), "lines") == ["https://missfrugalmommy.com/feed/"]
    bare = render_entries([survivor("https://a.example/f", "A")], "C", date(2026, 1, 1))
    assert "# about:" not in bare and "# recent posts: none with a link\n" in bare


def test_hostile_text_cannot_add_a_url_line(tmp_path: Path) -> None:
    hostile = "Nice blog\nhttps://evil.example/feed\r\u2028https://evil2.example/\x00\x1b[31m"
    posts = (
        RecentPost(
            "https://evil4.example/\nhttps://evil5.example/", "https://evil6.example/", hostile
        ),
    )
    entry = survivor("https://a.example/feed", hostile, language="x\nhttps://evil3.example/")
    block = render_entries(
        [replace(entry, subtitle="https://evil7.example/", recent_posts=posts)],
        "CC-MAIN-2021-49",
        date(2026, 11, 18),
    )
    assert extract_urls(block.encode(), "lines") == []
    assert all(line.startswith("#") or not line for line in block.splitlines())
    path = tmp_path / "survivors.txt"
    path.write_text(block)
    assert known_urls(path) == {"https://a.example/feed"}  # only the proposal's own line


def test_one_line() -> None:
    assert _one_line("  a\tb\n\nc  ", 50) == "a b c"
    assert _one_line("x" * 120, 100) == "x" * 99 + "…"
    assert _one_line(None, 10) == ""
    block = render_entries([survivor("https://a.example/f", None, None)], "C", date(2026, 1, 1))
    assert "# (no title) · (no language) · last post 2026-11-12" in block


def test_report_is_one_section_per_feed_with_untrusted_text_in_code_spans() -> None:
    hostile = survivor("https://a.example/feed", "@everyone see #12 | `x` [click](https://evil)")
    report = render_report(
        [hostile, survivor("https://b.example/feed", "B")],
        "CC-MAIN-2021-49",
        "5 slices",
        NOW,
    )
    assert "**2 feeds** from `CC-MAIN-2021-49`" in report and "|" not in report.replace(
        "@everyone see #12 | ", ""
    )  # no table
    first = report.split("### ")[1].splitlines()
    assert first == [
        "`@everyone see #12 | 'x' [click](https://evil)`",
        "",
        "- feed: `https://a.example/feed`",
        "- last post: 2026-11-12",
        "- languages: `en`",
        "",
    ]
    assert "latest posts" not in report  # they are in survivors.txt


def test_report_never_exceeds_the_github_limit() -> None:
    many = [survivor(f"https://{i}.example/{'a' * 200}/feed", "T" * 200) for i in range(1000)]
    report = render_report(many, "CC-MAIN-2021-49", "summary", NOW)
    assert len(report) <= _MAX_REPORT_CHARS
    assert "<opml" not in report  # too big: left out, the room goes to the feeds
    shown = report.count("### ")
    assert 0 < shown < 1000 and f"... and {1000 - shown} more feeds: see the diff." in report


def test_known_urls(tmp_path: Path) -> None:
    path = tmp_path / "survivors.txt"
    assert known_urls(path) == set()
    path.write_text(
        "# comment\nhttps://a.example/feed\n\n# x\nhttps://b.example/feed\n"
        "# https://c.example/feed\n"  # proposed, not accepted: still known
        "#   - https://c.example/post\n#     https://d.example/\n"  # a post and its title
        "# found in https://data.commoncrawl.org/x.warc.wat.gz\n"
    )
    assert known_urls(path) == {
        "https://a.example/feed",
        "https://b.example/feed",
        "https://c.example/feed",
    }


def test_log_line_is_one_line() -> None:
    line = log_line(survivor("https://a.example/feed", "Title\nhttps://x.example/\x1b[2J"))
    assert line == "  + https://a.example/feed  2026-11-12  Title https://x.example/ [2J"
    assert log_line(survivor("https://a.example/feed", None)).endswith("(no title)")


def test_acceptance() -> None:
    assert acceptance(104, 11944) == "0.87% (104 of 11944)"
    assert acceptance(0, 500) == "0.00% (0 of 500)"
    assert acceptance(0, 0) == "n/a (0 of 0)"


def test_opml_lists_every_proposal_in_order_escaped() -> None:
    feeds = [
        survivor("https://a.example/feed?x=1&y=2", 'Tom & "Jerry" <b>\x00\x1b\n'),
        survivor("https://b.example/rss", None),
    ]
    xml = _render_opml(feeds, "CC-MAIN-2021-49", NOW)
    root = SafeET.fromstring(xml.encode())  # well-formed: control characters are gone
    assert root.findtext("./head/title") == "Proposed feeds from CC-MAIN-2021-49"
    outlines = root.findall("./body/outline")
    assert [o.get("xmlUrl") for o in outlines] == [f.url for f in feeds]
    assert outlines[0].get("title") == outlines[0].get("text") == 'Tom & "Jerry" <b>'
    assert outlines[1].get("title") == "https://b.example/rss"  # no title: the URL
    assert "<b>" not in xml  # escaped, never markup


def test_report_starts_with_the_opml_in_a_fence_nothing_can_close() -> None:
    feeds = [
        survivor("https://a.example/feed", "````` break out"),
        survivor("https://b.example/f", "B"),
    ]
    report = render_report(feeds, "CC-MAIN-2021-49", "summary", NOW)
    assert report.index("<opml") < report.index("### ")  # before the sections
    fence = "``````"  # longer than the title's run of five
    start = report.index(fence + "xml\n") + len(fence + "xml\n")
    end = report.index(fence + "\n", start)
    assert report[start:end] == _render_opml(feeds, "CC-MAIN-2021-49", NOW)
    assert "<details><summary>" in report and "</details>" in report
