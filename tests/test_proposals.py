"""Rendering survivors' proposals: the proposal file and the pull request summary."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from defusedxml import ElementTree as SafeET

from indexer.check.feed import RecentPost
from indexer.sources import extract_urls
from indexer.survivors.proposals import (
    _one_line,
    acceptance,
    file_name,
    known_urls,
    log_line,
    render_entries,
    render_opml,
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
        [
            replace(
                frugal,
                subtitle="Saving money with kids",
                recent_posts=posts,
                categories=(("budgeting", 12), ("kids", 9)),
                posts_read=30,
            )
        ],
        "CC-MAIN-2021-49",
        date(2026, 11, 18),
    )
    assert block == (
        "# Proposed 2026-11-18 from CC-MAIN-2021-49. Uncomment the\n"
        "# URL of each feed to keep.\n"
        "\n# Miss Frugal Mommy · en · last post 2026-11-12\n"
        "# about: Saving money with kids\n"
        "# categories (on 30 posts): budgeting 12, kids 9\n"
        "# found in\n"
        f"#   {WAT}\n"
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


def test_comments_are_wrapped_for_phones() -> None:
    about = "Saving money with kids, one small habit at a time, every single week of the year"
    long_title = "We spend too much on food. Here are ten things that we changed to spend less"
    posts = (RecentPost("https://a.example/" + "x" * 80, long_title, None),)
    entry = replace(survivor("https://a.example/f", "A"), subtitle=about, recent_posts=posts)
    block = render_entries([entry], "C", date(2026, 1, 1))
    assert "# about: Saving money with kids, one small habit at a time,\n#   every" in block
    assert "#     We spend too much on food. Here are ten things that we\n#     changed" in block
    assert "#   - https://a.example/" + "x" * 80 + "\n" in block  # never broken
    assert all(len(line) <= 60 for line in block.splitlines() if "https://" not in line)


def test_hostile_text_cannot_add_a_url_line(tmp_path: Path) -> None:
    hostile = "Nice blog\nhttps://evil.example/feed\r\u2028https://evil2.example/\x00\x1b[31m"
    posts = (
        RecentPost(
            "https://evil4.example/\nhttps://evil5.example/", "https://evil6.example/", hostile
        ),
    )
    entry = survivor("https://a.example/feed", hostile, language="x\nhttps://evil3.example/")
    block = render_entries(
        [replace(entry, subtitle="x " * 30 + "https://evil7.example/", recent_posts=posts)],
        "CC-MAIN-2021-49",
        date(2026, 11, 18),
    )
    assert extract_urls(block.encode(), "lines") == []
    assert all(line.startswith("#") or not line for line in block.splitlines())
    (tmp_path / "202611").mkdir()
    (tmp_path / "202611" / "18-041105.txt").write_text(block)
    assert known_urls(tmp_path) == {"https://a.example/feed"}  # only the proposal's own line


def test_one_line() -> None:
    assert _one_line("  a\tb\n\nc  ", 50) == "a b c"
    assert _one_line("x" * 120, 100) == "x" * 99 + "…"
    assert _one_line(None, 10) == ""
    block = render_entries([survivor("https://a.example/f", None, None)], "C", date(2026, 1, 1))
    assert "# (no title) · (no language) · last post 2026-11-12" in block


def test_report_is_only_the_summary() -> None:  # the feeds are reviewed in the file
    report = render_report(
        2,
        "CC-MAIN-2021-49",
        ["25 MB of x.warc.wat.gz", "900 feed URLs (0 already known)"],
        [(1, "without a title"), (4, "with an SEO footer")],
    )
    assert report == (
        "**2 feeds** from `CC-MAIN-2021-49`, still active today.\n\n**Run**\n\n"
        "- 25 MB of x.warc.wat.gz\n- 900 feed URLs (0 already known)\n\n"
        "**Rejected by the rules**\n\n- 4 with an SEO footer\n- 1 without a title\n"
    )


def test_report_without_rejections() -> None:
    assert render_report(1, "C", ["10 checked"], []).endswith(
        "**Rejected by the rules**\n\n- none\n"
    )


def test_categories_line() -> None:
    hostile = replace(
        survivor("https://a.example/feed", "A"),
        categories=(("news\n# https://evil.example/", 3), ("uncategorized", 30)),
        posts_read=30,
    )
    block = render_entries([hostile], "C", date(2026, 1, 1))
    assert "# categories (on 30 posts): news #" in block
    assert "# https://evil.example/" not in block.splitlines()  # never a proposal's URL line
    assert "uncategorized 30" in block
    none = replace(survivor("https://b.example/feed", "B"), posts_read=5)
    assert "# categories (on 5 posts): none\n" in render_entries([none], "C", date(2026, 1, 1))


def test_known_urls_from_every_file_of_the_folder(tmp_path: Path) -> None:
    assert known_urls(tmp_path) == set()
    (tmp_path / "202610").mkdir()
    (tmp_path / "202611").mkdir()
    (tmp_path / "202610" / "11-041105.txt").write_text("# comment\nhttps://a.example/feed\n")
    (tmp_path / "202611" / "18-041105.txt").write_text(
        "# x\nhttps://b.example/feed\n"
        "# https://c.example/feed\n"  # proposed, not accepted: still known
        "#   - https://c.example/post\n#     https://d.example/\n"  # a post and its title
        "# found in https://data.commoncrawl.org/x.warc.wat.gz\n"
    )
    (tmp_path / ".gitkeep").write_text("https://e.example/feed\n")  # not a .txt file
    assert known_urls(tmp_path) == {
        "https://a.example/feed",
        "https://b.example/feed",
        "https://c.example/feed",
    }


def test_file_name_is_the_utc_start_time() -> None:
    from datetime import timedelta, timezone

    when = datetime(2026, 11, 21, 6, 11, 5, tzinfo=timezone(timedelta(hours=2)))
    assert file_name(when) == "202611/21-041105.txt"


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
    xml = render_opml(feeds, "CC-MAIN-2021-49", NOW)
    root = SafeET.fromstring(xml.encode())  # well-formed: control characters are gone
    assert root.findtext("./head/title") == "Proposed feeds from CC-MAIN-2021-49"
    outlines = root.findall("./body/outline")
    assert [o.get("xmlUrl") for o in outlines] == [f.url for f in feeds]
    assert outlines[0].get("title") == outlines[0].get("text") == 'Tom & "Jerry" <b>'
    assert outlines[1].get("title") == "https://b.example/rss"  # no title: the URL
    assert "<b>" not in xml  # escaped, never markup
