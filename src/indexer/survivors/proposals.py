"""Feeds found by `indexer survivors`, proposed for review in data/survivors.txt.

Survivors does not insert into the database. It appends its finds to data/survivors.txt (a
`lines` source in sources.toml, like submissions.txt), and the workflow proposes the change as a
pull request. A reviewer deletes the URLs they don't want and merges; the daily input job then
loads the file.

Each entry is one comment line with what a reviewer needs, then the URL:

    # Miss Frugal Mommy · en · last post 2026-11-12
    # found in https://data.commoncrawl.org/crawl-data/CC-MAIN-2021-49/.../...warc.wat.gz
    https://missfrugalmommy.com/feed/

Titles and links come from the feeds, so they are untrusted: in the file they are flattened to a
single line of printable text (a title can never start a new, uncommented line), and in the
Markdown report they are shown in code spans, where GitHub neither renders markup nor turns
@names into mentions or #numbers into links.
"""

import unicodedata
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from indexer.sources import extract_urls
from indexer.survivors.commoncrawl import Survivor

MAX_TITLE = 100


def one_line(text: str | None, limit: int) -> str:
    """Untrusted text as one line of printable characters, at most `limit` long."""
    printable = "".join(
        c if unicodedata.category(c)[0] not in "CZ" or c == " " else " " for c in text or ""
    )
    flat = " ".join(printable.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def known_urls(path: Path) -> set[str]:
    """The URLs already in the file (comments ignored); none if it doesn't exist."""
    try:
        return set(extract_urls(path.read_bytes(), "lines"))
    except FileNotFoundError:
        return set()


def render_entries(survivors: Iterable[Survivor], crawl: str, today: date) -> str:
    """A block to append to data/survivors.txt."""
    out = [f"\n# --- {today.isoformat()}, from {crawl} ---\n"]
    for s in survivors:
        summary = " · ".join(
            [
                one_line(s.title, MAX_TITLE) or "(no title)",
                one_line(s.language, 200) or "(no language)",
                f"last post {s.last_item_date.date().isoformat()}",
            ]
        )
        out.append(f"# {summary}\n# found in {s.wat_url}\n{s.url}\n")
    return "".join(out)


def log_line(s: Survivor) -> str:
    """One line for the run's log: the URL, the newest item's date and the title."""
    title = one_line(s.title, MAX_TITLE) or "(no title)"
    return f"  + {s.url}  {s.last_item_date.date().isoformat()}  {title}"


def _code(text: str) -> str:
    """Untrusted text as an inline code span: rendered literally, so no links, mentions or
    markup. Backticks inside would end the span, so they become quotes."""
    return "`" + text.replace("`", "'") + "`" if text else "—"


def acceptance(proposed: int, checked: int) -> str:
    """The share of the checked feed URLs that were proposed, e.g. "0.87% (104 of 11944)"."""
    share = f"{100 * proposed / checked:.2f}%" if checked else "n/a"
    return f"{share} ({proposed} of {checked})"


# GitHub rejects a pull request body or comment over 65,536 characters; the workflow adds a
# short introduction to the first one.
MAX_REPORT_CHARS = 60_000


def _feed_section(s: Survivor) -> str:
    lines = [
        f"### {_code(one_line(s.title, MAX_TITLE))}",
        "",
        f"- feed: {_code(s.url)}",
        f"- last post: {s.last_item_date.date().isoformat()}",
        f"- languages: {_code(one_line(s.language, 200))}",
        "- latest posts:" if s.item_links else "- latest posts: none with a link",
    ]
    lines += [f"  - {_code(one_line(link, 300))}" for link in s.item_links]
    return "\n".join(lines) + "\n\n"


def render_report(survivors: Sequence[Survivor], crawl: str, summary: str) -> str:
    """Markdown for the pull request: one section per proposed feed with its title, URL, last
    post, languages and the links of its latest posts, all as code spans (nothing clickable),
    to make spam easy to spot. Never longer than MAX_REPORT_CHARS: feeds that don't fit are
    counted at the end (they are all in the diff)."""
    report = f"**{len(survivors)} feeds** from `{crawl}`, still active today. {summary}\n\n"
    for shown, s in enumerate(survivors):
        section = _feed_section(s)
        if len(report) + len(section) > MAX_REPORT_CHARS - 200:  # room for the note below
            return report + f"... and {len(survivors) - shown} more feeds: see the diff.\n"
        report += section
    return report
