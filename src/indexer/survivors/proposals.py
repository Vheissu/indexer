"""Feeds found by `indexer survivors`, proposed for review in data/survivors/<timestamp>.txt.

Survivors does not insert into the database. Each run writes its finds to a new file in
data/survivors/<year><month>/ (the `lines` source `survivors/*/*.txt` in sources.toml), so no file
grows huge and two open pull requests never conflict; the workflow proposes the file as a pull
request. Each entry is comment lines with what a reviewer needs, then the feed's URL,
commented out too:

    # Miss Frugal Mommy · en · last post 2026-11-12
    # about: Saving money with kids, one small habit at a time,
    #   every week
    # categories (on 30 posts): budgeting 12, kids 9, recipes 4
    # found in
    #   https://data.commoncrawl.org/crawl-data/CC-MAIN-2021-49/.../...warc.wat.gz
    # recent posts:
    #   - https://missfrugalmommy.com/ten-ways/
    #     Ten ways to save on groceries
    #     We spend too much on food. Here are ten things that we
    #     changed...
    # https://missfrugalmommy.com/feed/

Comment text is wrapped at 60 characters, for reading the file on a phone.

A reviewer uncomments the URL line of each feed they want (deletes "# ") and merges; the daily
input job then loads the uncommented URLs. Commented-out proposals still count as known, so a
feed left out is not proposed again.

Titles, categories and links come from the feeds, so they are untrusted: in the file they are
flattened to a single line of printable text (a title can never start a new, uncommented line).
The pull request only says how many feeds were proposed and the run's summary. Next to the .txt
file, a run also writes the feeds as OPML (<dd-HHMMSS>.opml, escaped XML attributes; not read as
a source), which the pull request links to, for trying them in a feed reader while reviewing.
"""

import re
import textwrap
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from indexer.sources import extract_urls
from indexer.survivors.run import Survivor

_MAX_TITLE = 100
_MAX_SUBTITLE = 200
_WIDTH = 60  # characters per comment line in the file, for phones


# A proposal's URL line, commented out: "# " then the URL, alone on the line. Untrusted text is
# never written that way (it always follows a label or an indent).
_PROPOSED = re.compile(r"# (https?://\S+)")


def known_urls(directory: Path) -> set[str]:
    """The URLs already in the .txt files under the folder, accepted or commented out."""
    known: set[str] = set()
    for path in sorted(directory.rglob("*.txt")):
        data = path.read_bytes()
        known.update(extract_urls(data, "lines"))
        for line in data.decode("utf-8", errors="replace").splitlines():
            if m := _PROPOSED.fullmatch(line.strip()):
                known.add(m.group(1))
    return known


def file_name(when: datetime) -> str:
    """A run's file under data/survivors/, from its UTC start time: 202611/21-041105.txt."""
    return when.astimezone(UTC).strftime("%Y%m/%d-%H%M%S") + ".txt"


def render_entries(survivors: Iterable[Survivor], crawl: str, today: date) -> str:
    """A run's file in data/survivors/. Comments are wrapped at _WIDTH for reading on a phone;
    URLs are never broken, and the feed's own URL line stays whole."""
    out = [
        _comment(
            f"Proposed {today.isoformat()} from {crawl}. Uncomment the URL of each feed to keep.",
            "# ",
        )
    ]
    for s in survivors:
        summary = " · ".join(
            [
                _one_line(s.title, _MAX_TITLE) or "(no title)",
                _one_line(s.language, 200) or "(no language)",
                f"last post {s.last_item_date.date().isoformat()}",
            ]
        )
        out.append("\n" + _comment(summary))
        if s.score is not None:
            out.append(f"# score: {s.score:.3f}\n")
        if subtitle := _one_line(s.subtitle, _MAX_SUBTITLE):
            out.append(_comment(f"about: {subtitle}"))
        if s.posts_read:
            names = ", ".join(f"{_one_line(name, 50)} {n}" for name, n in s.categories) or "none"
            out.append(_comment(f"categories (on {s.posts_read} posts): {names}"))
        out.append(_comment(f"found in {s.wat_url}"))
        out.append("# recent posts:\n" if s.recent_posts else "# recent posts: none with a link\n")
        for post in s.recent_posts:
            out.append(_comment(_one_line(post.link, 300), "#     ", "#   - "))
            for text in (_one_line(post.title, _MAX_TITLE), _one_line(post.description, 200)):
                if text:
                    out.append(_comment(text, "#     ", "#     "))
        out.append(f"# {s.url}\n")
    return "".join(out)


def log_line(s: Survivor) -> str:
    """One line for the run's log: the URL, the newest item's date and the title."""
    title = _one_line(s.title, _MAX_TITLE) or "(no title)"
    return f"  + {s.url}  {s.last_item_date.date().isoformat()}  {title}"


def acceptance(proposed: int, checked: int) -> str:
    """The share of the checked feed URLs that were proposed, e.g. "0.87% (104 of 11944)"."""
    share = f"{100 * proposed / checked:.2f}%" if checked else "n/a"
    return f"{share} ({proposed} of {checked})"


def render_report(
    proposed: int, crawl: str, steps: Sequence[str], rejected: Sequence[tuple[int, str]]
) -> str:
    """Markdown for the pull request: how many feeds were proposed, the run's steps and what each
    rule rejected (most first, zero counts left out), as bullet lists. The feeds themselves are
    reviewed in the proposal file (untrusted text stays out of the PR)."""
    lines = [f"**{proposed} feeds** from `{crawl}`, still active today.", "", "**Run**", ""]
    lines += [f"- {step}" for step in steps]
    lines += ["", "**Rejected by the rules**", ""]
    by_count = sorted(rejected, key=lambda r: -r[0])
    lines += [f"- {n} {label}" for n, label in by_count] or ["- none"]
    return "\n".join(lines) + "\n"


def render_opml(survivors: Iterable[Survivor], crawl: str, generated: datetime) -> str:
    """The proposed feeds as OPML (the .opml file next to the run's .txt), in proposal order,
    for importing into a feed reader during review. Titles are flattened to one printable line
    (no control characters, which XML forbids) and every value is escaped."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{escape(f'Proposed feeds from {crawl}')}</title>",
        f"    <dateCreated>{format_datetime(generated)}</dateCreated>",
        "  </head>",
        "  <body>",
    ]
    for s in survivors:
        title = quoteattr(_one_line(s.title, _MAX_TITLE) or s.url)
        lines.append(
            f'    <outline type="rss" text={title} title={title} xmlUrl={quoteattr(s.url)}/>'
        )
    lines += ["  </body>", "</opml>"]
    return "\n".join(lines) + "\n"


def _one_line(text: str | None, limit: int) -> str:
    """Untrusted text as one line of printable characters, at most `limit` long."""
    printable = "".join(
        c if unicodedata.category(c)[0] not in "CZ" or c == " " else " " for c in text or ""
    )
    flat = " ".join(printable.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _comment(text: str, rest: str = "#   ", first: str = "# ") -> str:
    """Comment lines holding `text` (one line of printable text), wrapped at _WIDTH. Later lines
    start with `rest`, which for untrusted text is "#" and more than one space, so no line of it
    can look like a proposal's URL line (_PROPOSED)."""
    lines = textwrap.wrap(
        text,
        width=_WIDTH,
        initial_indent=first,
        subsequent_indent=rest,
        break_long_words=False,  # a URL stays whole
        break_on_hyphens=False,
    )
    return "".join(line + "\n" for line in lines)
