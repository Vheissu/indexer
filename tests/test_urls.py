"""Shared candidate-URL handling: validation, de-duplication and domain exclusion."""

import pytest

from indexer.net.urls import drop_excluded, strip_session, valid_urls


def test_valid_urls_canonicalizes_dedupes_and_counts_invalid() -> None:
    valid, invalid = valid_urls(
        [
            "https://A.example/feed",
            "https://a.example/feed",  # same URL once the host is lower-cased
            "ftp://a.example/feed",
            "http://127.0.0.1/feed",  # a non-public IP literal
            "https://user:pw@a.example/",
            "not a url",
            "https://b.example:443/rss",
        ]
    )
    assert valid == [
        ("https://a.example/feed", "a.example"),
        ("https://b.example/rss", "b.example"),
    ]
    assert invalid == 4


def test_drop_excluded_matches_registrable_domains_only() -> None:
    valid, _ = valid_urls(
        [
            "https://www.youtube.com/feeds/videos.xml?channel_id=1",
            "https://old.reddit.com/.rss",
            "https://notreddit.com/feed",
            "https://reddit.com.evil.example/feed",
        ]
    )
    kept, dropped = drop_excluded(valid, frozenset({"youtube.com", "reddit.com"}))
    assert [url for url, _ in kept] == [
        "https://notreddit.com/feed",
        "https://reddit.com.evil.example/feed",
    ]
    assert dropped == 2
    assert drop_excluded(valid, frozenset()) == (valid, 0)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://gb64.com/forum/app.php/feed?sid=4a54a14464f863ff21a8145560535d65",
            "https://gb64.com/forum/app.php/feed",
        ),
        ("https://x.example/f.php?a=1&PHPSESSID=abc&b=%20", "https://x.example/f.php?a=1&b=%20"),
        ("https://x.example/feed;jsessionid=ABC123?x=1", "https://x.example/feed?x=1"),
        ("https://x.example/f?session_id=1&sessionid=2", "https://x.example/f"),
        # Untouched: no session parameter, odd queries kept byte for byte.
        ("https://x.example/index.php?forums/a.14/index.rss", None),
        ("https://x.example/feed?side=1&sids=2", None),
        ("https://x.example/feed/", None),
    ],
)
def test_strip_session(url: str, expected: str | None) -> None:
    assert strip_session(url) == (expected or url)
