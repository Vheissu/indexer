"""Shared candidate-URL handling: validation, de-duplication and domain exclusion."""

from indexer.net.urls import drop_excluded, valid_urls


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
