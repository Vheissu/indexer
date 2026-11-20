import gzip
from urllib.parse import urljoin, urlsplit

import re
import json

from urllib.parse import urlsplit

FILE = "slice.warc.wat.gz"
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}


def read_record(stream):
    line = stream.readline()
    while line and not line.startswith(b"WARC/"):
        line = stream.readline()
    if not line:
        return None
    length = 0
    while header := stream.readline().strip():
        if header.lower().startswith(b"content-length:"):
            length = int(header.split(b":")[1])
    return stream.read(length)


ok_patterns = [
    re.compile(r"/(atom|rss|rss2|feed|feeds)(.feed.php)?/?"),
    re.compile(r"/(atom|rss|rss2|feed|feeds)\.xml"),
    re.compile(r"/recent.rss"),
    re.compile(r"/\?rss=1"),
    re.compile(r"\?com=rss"),
    re.compile(r"/(atom|rss|rss2|feed|feeds).php"),
    re.compile(r"/xml.php?rss=true"),
    re.compile(r"/app.php/feed"),
    re.compile(r"/(atom|rss|feed|feeds)(/|\.)[a-zA-Z0-9_.-]+/?"),
    re.compile(r"/[a-zA-Z0-9_.-]+(/|\.)(atom|rss|feed|feeds)/?"),
]

rejected_patterns = [
    re.compile(r"/20[0-9]{2}/[0-9]{2}/"),
    re.compile(r"/comments"),
    re.compile(r"/sitemap.xml"),
    re.compile(r"comments/"),
    re.compile(r"feed=comments"),
    re.compile(r"commentId="),
    re.compile(r"/comment/"),
    re.compile(r":Recent"),
    re.compile(r"alt=rss"),
]

feed_indicators = re.compile(r"(atom|rss|feed|rdf|xml|spip|synd|pod|api|backend.php)", re.IGNORECASE)

def filter_link(page, link):

    try:
        if link.get("type") not in FEED_TYPES:
            return

        url = urljoin(page, link.get("url"))
        parts = urlsplit(url)
        url = parts._replace(scheme="https").geturl()
        parts = urlsplit(url)

        if len(url) > 256:
            return

        if not feed_indicators.search(url):
            return

        beyond_domain = url[len(parts.scheme) + 3 + len(parts.netloc):]

        if any(pattern.search(url) for pattern in rejected_patterns):
            return None

        slashes_count = parts.path.count("/")

        if slashes_count > 7:
            return

        longest = max(len(segment) for segment in parts.path.split("/"))
        if longest > 25:
            return

        if any(pattern.fullmatch(beyond_domain) for pattern in ok_patterns):
            return url
    except:
        # TODO: log something
        pass

    return

def filter_page_links(page, links):
    new_links = []

    for link in links:
        normalized_link = filter_link(page, link)
        if normalized_link:
            new_links.append(normalized_link)

    return new_links


with gzip.open(FILE, "rb") as stream:
    pages = {}

    try:
        while (body := read_record(stream)) is not None:
            try:
                envelope = json.loads(body)["Envelope"]

                links = envelope["Payload-Metadata"]["HTTP-Response-Metadata"]["HTML-Metadata"]["Head"]["Link"]

                page = envelope["WARC-Header-Metadata"]["WARC-Target-URI"]
            except (ValueError, KeyError):
                continue

            feed_links = filter_page_links(page, links)

            if feed_links:
                pages[page] = feed_links

    except EOFError:
        pass

for p in pages:
    print(p)
    print(pages[p])
    print()
    print()
