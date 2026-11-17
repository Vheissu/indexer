"""Decide whether a body is an RSS/Atom feed and extract item count, newest date, ttl, the
generator, the URLs the feed gives for itself (site, self, new-feed-url) and descriptive fields
for labelling (title, subtitle, categories, language, media type, icon, logo, rights, license).

Parsing uses defusedxml: entity declarations and external references are refused, and DTDs
are never loaded. A bare <!DOCTYPE> (common in RSS 0.91) is tolerated because nothing is
fetched or expanded from it. The Content-Type header is ignored; only the parse result counts.
"""

import dataclasses
import email.utils
import html
import re
import unicodedata
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from indexer.errors import CheckError, ErrorCode

ATOM = "{http://www.w3.org/2005/Atom}"
ATOM03 = "{http://purl.org/atom/ns#}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RSS10 = "{http://purl.org/rss/1.0/}"
RSS090 = "{http://my.netscape.com/rdf/simple/0.9/}"
DC = "{http://purl.org/dc/elements/1.1/}"
DCTERMS = "{http://purl.org/dc/terms/}"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
ADMIN = "{http://webns.net/mvcb/}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
CC = "{http://backend.userland.com/creativeCommonsRssModule}"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

MAX_URL_LEN = 2048
MAX_TEXT_LEN = 200
MAX_SUBTITLE_LEN = 500
MAX_CATEGORY_LEN = 100  # one itunes:category path
MAX_CATEGORIES = 50  # itunes:category paths
MAX_CATEGORY_LIST_LEN = 2048  # the whole categories / item_categories value
MAX_ITEM_LINKS = 3  # links of the most recent items (for reviewing proposals; not stored)
MAX_ITEM_LINK_LEN = 300
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".webm", ".mov", ".mkv")
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{1,8})*")
_TAG = re.compile(r"<[^>]*>")

MAX_INT32 = 2**31 - 1


@dataclass(frozen=True, slots=True)
class FeedInfo:
    kind: str  # rss, rdf, atom, atom03
    item_count: int
    last_item_date: datetime | None
    ttl: int | None
    last_tagged_item_date: datetime | None = None  # newest item with a category / tag
    generator: str | None = None
    site_url: str | None = None  # the website the feed belongs to
    self_url: str | None = None  # where the feed says it lives (atom:link rel=self)
    new_feed_url: str | None = None  # <itunes:new-feed-url>: the feed has moved
    # Descriptive fields, named after their Atom counterparts.
    title: str | None = None
    subtitle: str | None = None  # Atom <subtitle>, RSS <description>
    categories: str | None = None  # feed-level categories, normalized, comma separated, sorted
    item_categories: str | None = None  # all items' categories, same format
    # Each item's link as written in the feed (not resolved or validated: untrusted text, shown
    # to reviewers, never fetched), distinct: the MAX_ITEM_LINKS most recent items, newest first.
    item_links: tuple[str, ...] = ()
    itunes_category: str | None = None  # "Technology > Software, ...", sorted
    language: str | None = None  # lower-case language tag
    media_type: str | None = None  # text, podcast or video; None for an empty feed
    icon: str | None = None
    logo: str | None = None  # Atom <logo>, RSS <image><url>, <itunes:image>
    rights: str | None = None  # Atom <rights>, RSS <copyright>, dc:rights
    license: str | None = None  # link rel=license, creativeCommons:license


def clean_url(value: str | None, base: str | None = None) -> str | None:
    """An absolute http(s) URL that is safe to store, or None. Nothing here is fetched."""
    if not value or not (value := value.strip()) or len(value) > MAX_URL_LEN:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    try:
        joined = urllib.parse.urljoin(base, value) if base else value
        scheme = urllib.parse.urlsplit(joined).scheme.lower()
    except ValueError:
        return None
    if scheme not in ("http", "https") or len(joined) > MAX_URL_LEN:
        return None
    return joined


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return text[:MAX_TEXT_LEN] or None


def _atom_links(parent: Element, ns: str, base: str | None) -> tuple[str | None, str | None]:
    """(site_url, self_url) from <link> children; rel defaults to alternate."""
    site = self_ = None
    for link in parent.findall(f"{ns}link"):
        rel = (link.get("rel") or "alternate").strip().lower()
        href = clean_url(link.get("href"), base)
        if href is None:
            continue
        if rel == "self" and self_ is None:
            self_ = href
        elif rel == "alternate" and site is None:
            site = href
    return site, self_


def _atom_generator(root: Element, ns: str) -> str | None:
    gen = root.find(f"{ns}generator")
    if gen is None:
        return None
    text = (gen.text or "").strip()
    version = (gen.get("version") or "").strip()
    if version and version not in text:
        text = f"{text} {version}"
    return _clean_text(text)


def parse_date(text: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom, Dublin Core). Naive times are taken as UTC."""
    if not text or not (value := text.strip()):
        return None
    parsers = (_iso, _rfc822) if value[:4].isdigit() else (_rfc822, _iso)
    for parser in parsers:
        try:
            dt = parser(value)
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
        if dt is not None:
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


def _rfc822(value: str) -> datetime | None:
    return email.utils.parsedate_to_datetime(value)


def _iso(value: str) -> datetime | None:
    return datetime.fromisoformat(value)


def _newest(items: Iterable[Element], tags: tuple[str, ...]) -> datetime | None:
    newest: datetime | None = None
    for item in items:
        for tag in tags:
            dt = parse_date(item.findtext(tag))
            if dt is not None and (newest is None or dt > newest):
                newest = dt
    return newest


# Where each format keeps an item's date.
_RSS_DATES = ("pubDate", f"{DC}date", f"{ATOM}updated", f"{ATOM}published")
_ATOM_DATES = (f"{ATOM}updated", f"{ATOM}published")
_ATOM03_DATES = (f"{ATOM03}modified", f"{ATOM03}issued", f"{ATOM03}created")
_RDF_DATES = (f"{DC}date", f"{DCTERMS}modified", f"{DCTERMS}issued")

# Platform defaults that say nothing about an item (WordPress files posts under "Uncategorized").
DEFAULT_CATEGORIES = frozenset({"uncategorized", "uncategorised", "general"})


def _newest_tagged(items: Iterable[Element], tags: tuple[str, ...]) -> datetime | None:
    """The newest date among items with at least one category (after normalization) that is
    not a platform default."""
    return _newest((i for i in items if _has_real_category(i)), tags)


def _has_real_category(item: Element) -> bool:
    return any(
        (term := normalize_category(part)) and term not in DEFAULT_CATEGORIES
        for value in _category_values([item])
        for part in (value or "").split(",")
    )


def _ttl(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if 0 <= value <= MAX_INT32 else None


def _looks_like_markup(body: bytes) -> bool:
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<")


def _looks_like_html(body: bytes) -> bool:
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")[:1024].lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head


_DECLARED_ENCODING = re.compile(
    rb"""^(?:\xef\xbb\xbf)?\s*<\?xml\s[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._:-]{1,40})["']"""
)


def _parse_xml(body: bytes) -> Element:
    return SafeET.fromstring(body, forbid_dtd=False, forbid_entities=True, forbid_external=True)


def _as_utf8(body: bytes) -> bytes:
    """The body re-encoded as UTF-8, for an encoding expat can't read itself (Shift_JIS, EUC-JP,
    GB2312, Big5, EUC-KR, ...: "multi-byte encodings are not supported"). Decoded with Python's
    codecs from just after the declared name, which is ASCII in all of them; the declaration then
    says UTF-8. Undecodable bytes become U+FFFD. The result is parsed with the same protections."""
    match = _DECLARED_ENCODING.match(body[:512])
    if match is None:
        raise CheckError(ErrorCode.PARSE_ERROR, "unsupported encoding")
    name = match.group(1).decode("ascii")
    try:
        rest = body[match.end(1) :].decode(name, errors="replace")
    except LookupError as exc:
        raise CheckError(ErrorCode.PARSE_ERROR, f"unknown encoding {name[:40]}") from exc
    return body[: match.start(1)] + b"UTF-8" + rest.encode("utf-8")


def parse_feed(body: bytes, base_url: str | None = None) -> FeedInfo:
    """Raise CheckError unless body is a feed. base_url resolves relative links."""
    if not _looks_like_markup(body):
        raise CheckError(ErrorCode.NOT_A_FEED, "body is not XML")
    try:
        try:
            root = _parse_xml(body)
        except DefusedXmlException:
            raise  # a ValueError too: handled below, never retried
        except (ValueError, LookupError):
            # Expat refuses a declared multi-byte or unknown encoding: re-encode and retry.
            root = _parse_xml(_as_utf8(body))
    except DefusedXmlException as exc:
        raise CheckError(ErrorCode.PARSE_ERROR, f"forbidden XML: {type(exc).__name__}") from exc
    except ParseError as exc:
        if _looks_like_html(body):
            raise CheckError(ErrorCode.NOT_A_FEED, "HTML document") from exc
        raise CheckError(ErrorCode.PARSE_ERROR, str(exc)) from exc

    info = _parse_root(root, base_url)
    return dataclasses.replace(info, **_describe(root, info.kind, base_url))


def _parse_root(root: Element, base_url: str | None) -> FeedInfo:
    tag = root.tag
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise CheckError(ErrorCode.NOT_A_FEED, "<rss> without <channel>")
        items = channel.findall("item") or root.findall("item")
        date_tags: tuple[str, ...] = _RSS_DATES
        _site, self_url = _atom_links(channel, ATOM, base_url)
        return FeedInfo(
            "rss",
            len(items),
            _newest(items, date_tags),
            _ttl(channel.findtext("ttl")),
            last_tagged_item_date=_newest_tagged(items, date_tags),
            generator=_clean_text(channel.findtext("generator")),
            site_url=clean_url(channel.findtext("link"), base_url),
            self_url=self_url,
            new_feed_url=clean_url(channel.findtext(f"{ITUNES}new-feed-url"), base_url),
        )
    if tag in (f"{ATOM}feed", f"{ATOM03}feed"):
        ns = ATOM if tag == f"{ATOM}feed" else ATOM03
        entries = root.findall(f"{ns}entry")
        date_tags = _ATOM_DATES if ns == ATOM else _ATOM03_DATES
        site, self_url = _atom_links(root, ns, base_url)
        return FeedInfo(
            "atom" if ns == ATOM else "atom03",
            len(entries),
            _newest(entries, date_tags),
            None,
            last_tagged_item_date=_newest_tagged(entries, date_tags),
            generator=_atom_generator(root, ns),
            site_url=site,
            self_url=self_url,
        )
    if tag == f"{RDF}RDF":
        ns = RSS10
        channel = root.find(f"{RSS10}channel")
        if channel is None:
            ns, channel = RSS090, root.find(f"{RSS090}channel")
        if channel is None:
            raise CheckError(ErrorCode.NOT_A_FEED, "RDF document without an RSS channel")
        items = root.findall(f"{RSS10}item") + root.findall(f"{RSS090}item")
        date_tags = _RDF_DATES
        agent = channel.find(f"{ADMIN}generatorAgent")
        return FeedInfo(
            "rdf",
            len(items),
            _newest(items, date_tags),
            None,
            last_tagged_item_date=_newest_tagged(items, date_tags),
            generator=_clean_text(agent.get(f"{RDF}resource")) if agent is not None else None,
            site_url=clean_url(channel.findtext(f"{ns}link"), base_url),
        )
    raise CheckError(ErrorCode.NOT_A_FEED, f"root element {tag[:100]}")


def _text_of(el: Element | None, *, markup: bool = False, limit: int = MAX_TEXT_LEN) -> str | None:
    """Plain text of an element. markup=True strips tags from escaped HTML (RSS descriptions,
    Atom type="html"); Atom type="xhtml" content is child elements, so itertext covers it."""
    if el is None:
        return None
    text = "".join(el.itertext())
    if markup or el.get("type") == "html":
        text = html.unescape(_TAG.sub(" ", text))
    cleaned = _clean_text(text)
    return cleaned[:limit] if cleaned else None


def _first(*values: str | None) -> str | None:
    return next((v for v in values if v), None)


def _language(*values: str | None) -> str | None:
    for value in values:
        tag = (value or "").strip().lower().replace("_", "-")
        if len(tag) <= 35 and _LANGUAGE.fullmatch(tag):
            return tag
    return None


def _join_sorted(values: Iterable[str]) -> str | None:
    """Comma-separated, de-duplicated ignoring case, sorted alphabetically ignoring case."""
    unique: dict[str, str] = {}
    for value in values:
        unique.setdefault(value.casefold(), value)
    ordered = sorted(unique.values(), key=lambda v: (v.casefold(), v))[:MAX_CATEGORIES]
    return ", ".join(ordered) or None


def normalize_category(value: str) -> str:
    """Lower-case ASCII letters, digits, '-', '_' and single spaces. '+' becomes 'plus' and
    '#' becomes 'sharp'; accents are folded (é -> e); everything else is dropped.
    'C++' -> 'cplusplus', 'C#' -> 'csharp', 'Node.js' -> 'nodejs', 'Café' -> 'cafe'."""
    value = value.replace("+", "plus").replace("#", "sharp")
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    kept = "".join(c for c in folded.lower() if c.isalnum() or c in "-_ \t\n\r")
    return " ".join(kept.split())


def _categories(raw: Iterable[str | None]) -> str | None:
    """Split each value on commas (some feeds put a list in one element), normalize,
    de-duplicate, sort, and join with ', ' up to MAX_CATEGORY_LIST_LEN
    characters. Only whole terms are kept, except a first term that alone is too long."""
    unique = {
        term
        for value in raw
        for part in (value or "").split(",")
        if (term := normalize_category(part))
    }
    out = ""
    for term in sorted(unique):
        candidate = f"{out}, {term}" if out else term[:MAX_CATEGORY_LIST_LEN]
        if len(candidate) > MAX_CATEGORY_LIST_LEN:
            break
        out = candidate
    return out or None


def _category_values(elements: Iterable[Element]) -> list[str | None]:
    """Values of <category> (text, or Atom term/label) and dc:subject children."""
    values: list[str | None] = []
    for parent in elements:
        for child in parent:
            if child.tag == "category":
                values.append(child.text)
            elif child.tag in (f"{ATOM}category", f"{ATOM03}category"):
                values.append(child.get("term") or child.get("label") or child.text)
            elif child.tag == f"{DC}subject":
                values.append(child.text)
    return values


def _itunes_categories(channel: Element) -> str | None:
    paths: list[str] = []
    for top in channel.findall(f"{ITUNES}category"):
        name = _clean_text((top.get("text") or "").replace(",", " "))
        if not name:
            continue
        subs = [
            sub_name
            for sub in top.findall(f"{ITUNES}category")
            if (sub_name := _clean_text((sub.get("text") or "").replace(",", " ")))
        ]
        paths.extend([f"{name} > {sub}" for sub in subs] or [name])
    return _join_sorted(p[:MAX_CATEGORY_LEN] for p in paths)


def _item_media(item: Element) -> str | None:
    """'video', 'audio' or None, from enclosures, Media RSS and YouTube elements."""
    found: set[str] = set()
    if item.find(f"{YT}videoId") is not None:
        return "video"
    candidates: list[tuple[str, str]] = []
    for enc in item.findall("enclosure"):
        candidates.append((enc.get("type") or "", enc.get("url") or ""))
    for ns in (ATOM, ATOM03):
        for link in item.findall(f"{ns}link"):
            if (link.get("rel") or "").strip().lower() == "enclosure":
                candidates.append((link.get("type") or "", link.get("href") or ""))
    for media in item.iter(f"{MEDIA}content"):
        medium = (media.get("medium") or "").lower()
        if medium in ("audio", "video"):
            found.add(medium)
        candidates.append((media.get("type") or "", media.get("url") or ""))
    for mime, href in candidates:
        major = mime.split("/")[0].strip().lower()
        try:
            path = urllib.parse.urlsplit(href.strip()).path.lower()
        except ValueError:
            path = ""
        if major == "video" or path.endswith(VIDEO_EXTENSIONS):
            found.add("video")
        elif major == "audio" or path.endswith(AUDIO_EXTENSIONS):
            found.add("audio")
    if "video" in found:
        return "video"
    return "audio" if "audio" in found else None


def _media_type(items: list[Element]) -> str | None:
    if not items:
        return None
    kinds = [_item_media(item) for item in items]
    if 2 * kinds.count("video") >= len(items):
        return "video"
    if 2 * kinds.count("audio") >= len(items):
        return "podcast"
    return "text"


def _rel_link(parent: Element, ns: str, rel: str, base: str | None) -> str | None:
    for link in parent.findall(f"{ns}link"):
        if (link.get("rel") or "").strip().lower() == rel:
            href = clean_url(link.get("href"), base)
            if href:
                return href
    return None


def _item_links(
    items: Iterable[Element], ns: str, atom: bool, date_tags: tuple[str, ...]
) -> tuple[str, ...]:
    """The links of the MAX_ITEM_LINKS most recent items (undated items after dated ones, in
    feed order), as written: RSS/RDF <link> text, Atom <link rel=alternate href>. Only
    printable characters kept and the length capped; otherwise as-is."""
    dated: list[tuple[float, int, str]] = []
    for position, item in enumerate(items):
        if atom:
            raw = next(
                (
                    link.get("href")
                    for link in item.findall(f"{ns}link")
                    if (link.get("rel") or "alternate").strip().lower() == "alternate"
                ),
                None,
            )
        else:
            raw = item.findtext(f"{ns}link")
        text = "".join(c for c in (raw or "") if c.isprintable()).strip()[:MAX_ITEM_LINK_LEN]
        if text:
            dates = [d for tag in date_tags if (d := parse_date(item.findtext(tag)))]
            newest = max(dates).timestamp() if dates else float("-inf")
            dated.append((-newest, position, text))
    found: dict[str, None] = {}
    for _newest, _position, text in sorted(dated):
        found.setdefault(text)
        if len(found) == MAX_ITEM_LINKS:
            break
    return tuple(found)


class Description(TypedDict):
    title: str | None
    subtitle: str | None
    categories: str | None
    item_categories: str | None
    item_links: tuple[str, ...]
    itunes_category: str | None
    language: str | None
    media_type: str | None
    icon: str | None
    logo: str | None
    rights: str | None
    license: str | None


def _describe(root: Element, kind: str, base: str | None) -> Description:
    """Descriptive fields; every value is cleaned, length-capped text or a cleaned URL."""
    if kind == "rss":
        channel = root.find("channel")
        assert channel is not None
        items = channel.findall("item") or root.findall("item")
        image = channel.find(f"{ITUNES}image")
        return {
            "title": _text_of(channel.find("title"), markup=True),
            "subtitle": _text_of(channel.find("description"), markup=True, limit=MAX_SUBTITLE_LEN),
            "categories": _categories(_category_values([channel])),
            "item_categories": _categories(_category_values(items)),
            "item_links": _item_links(items, "", atom=False, date_tags=_RSS_DATES),
            "itunes_category": _itunes_categories(channel),
            "language": _language(
                channel.findtext("language"),
                channel.findtext(f"{DC}language"),
                channel.get(XML_LANG),
                root.get(XML_LANG),
            ),
            "media_type": _media_type(items),
            "icon": clean_url(channel.findtext(f"{ATOM}icon"), base),
            "logo": _first(
                clean_url(channel.findtext("image/url"), base),
                clean_url(image.get("href"), base) if image is not None else None,
            ),
            "rights": _first(
                _text_of(channel.find("copyright")), _text_of(channel.find(f"{DC}rights"))
            ),
            "license": _first(
                clean_url(channel.findtext(f"{CC}license"), base),
                _rel_link(channel, ATOM, "license", base),
            ),
        }
    if kind in ("atom", "atom03"):
        ns = ATOM if kind == "atom" else ATOM03
        entries = root.findall(f"{ns}entry")
        return {
            "title": _text_of(root.find(f"{ns}title")),
            "subtitle": _text_of(
                root.find(f"{ns}subtitle") if ns == ATOM else root.find(f"{ns}tagline"),
                limit=MAX_SUBTITLE_LEN,
            ),
            "categories": _categories(_category_values([root])),
            "item_categories": _categories(_category_values(entries)),
            "item_links": _item_links(
                entries, ns, atom=True, date_tags=_ATOM_DATES if ns == ATOM else _ATOM03_DATES
            ),
            "itunes_category": _itunes_categories(root),
            "language": _language(root.get(XML_LANG)),
            "media_type": _media_type(entries),
            "icon": clean_url(root.findtext(f"{ns}icon"), base),
            "logo": clean_url(root.findtext(f"{ns}logo"), base),
            "rights": _text_of(
                root.find(f"{ns}rights") if ns == ATOM else root.find(f"{ns}copyright")
            ),
            "license": _rel_link(root, ns, "license", base),
        }
    # rdf
    ns = RSS10 if root.find(f"{RSS10}channel") is not None else RSS090
    channel = root.find(f"{ns}channel")
    assert channel is not None
    items = root.findall(f"{RSS10}item") + root.findall(f"{RSS090}item")
    links = _item_links(items, ns, atom=False, date_tags=_RDF_DATES)
    return {
        "title": _text_of(channel.find(f"{ns}title"), markup=True),
        "subtitle": _text_of(channel.find(f"{ns}description"), markup=True, limit=MAX_SUBTITLE_LEN),
        "categories": _categories(_category_values([channel])),
        "item_categories": _categories(_category_values(items)),
        "item_links": links[:MAX_ITEM_LINKS],
        "itunes_category": None,
        "language": _language(channel.findtext(f"{DC}language"), root.get(XML_LANG)),
        "media_type": _media_type(items),
        "icon": None,
        "logo": clean_url(root.findtext(f"{ns}image/{ns}url"), base),
        "rights": _text_of(channel.find(f"{DC}rights")),
        "license": clean_url(channel.findtext(f"{CC}license"), base),
    }
