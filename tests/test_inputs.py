from pathlib import Path

import pytest

from indexer.errors import CheckError, ErrorCode
from indexer.net.validate import read_url_file, validate_url


def code_of(raw: str) -> ErrorCode | None:
    try:
        validate_url(raw)
    except CheckError as exc:
        return exc.code
    return None


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com/feed",
        "https://example.com/feed.xml?x=1",
        "HTTPS://Example.COM/feed",
        "http://example.com:80/feed",
        "https://example.com:443/feed",
        "http://example.com:443/feed",  # 443 is an allowed port whatever the scheme
        "https://bücher.example/feed",
        "http://8.8.8.8/feed",
        "http://[2606:4700:4700::1111]/feed",
    ],
)
def test_accepted(raw: str) -> None:
    assert code_of(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com/feed",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/xml,<rss/>",
        "example.com/feed",
        "//example.com/feed",
        "feed://example.com/rss",
    ],
)
def test_scheme_rejected(raw: str) -> None:
    assert code_of(raw) is ErrorCode.INVALID_URL


@pytest.mark.parametrize(
    "raw",
    [
        "http://user:pass@example.com/feed",
        "http://user@example.com/feed",
        "https://:pass@example.com/feed",
        "http://@example.com/feed",
    ],
)
def test_credentials_rejected(raw: str) -> None:
    assert code_of(raw) is ErrorCode.INVALID_URL


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com:8080/feed",
        "https://example.com:8443/feed",
        "http://example.com:22/",
        "http://example.com:0/",
        "http://example.com:65535/",
        "http://example.com:99999/",
    ],
)
def test_port_rejected(raw: str) -> None:
    assert code_of(raw) is ErrorCode.INVALID_URL


@pytest.mark.parametrize(
    "raw",
    ["", "http://", "http:///feed", "http://exa mple.com/", "http://example.com/\nfeed"],
)
def test_malformed_rejected(raw: str) -> None:
    assert code_of(raw) is ErrorCode.INVALID_URL


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1/feed",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://0.0.0.0/",
    ],
)
def test_ip_literal_blocked(raw: str) -> None:
    assert code_of(raw) is ErrorCode.BLOCKED_IP


def test_fragment_dropped() -> None:
    assert str(validate_url("https://example.com/feed#top")) == "https://example.com/feed"


def test_read_url_file(tmp_path: Path) -> None:
    path = tmp_path / "urls.txt"
    path.write_text(
        "# comment\n\nhttps://a.example/feed\n   \n  https://b.example/rss  \n"
        "#https://commented.example/\nhttps://a.example/feed\nnot a url\n",
        encoding="utf-8",
    )
    assert read_url_file(path) == ["https://a.example/feed", "https://b.example/rss", "not a url"]
