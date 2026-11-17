"""Fixed error identifiers stored in health_checks.error."""

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_URL = "invalid_url"  # bad syntax, scheme, credentials or port
    BLOCKED_IP = "blocked_ip"  # host is, or resolves to, a non-public address
    DNS_NXDOMAIN = "dns_nxdomain"  # the name does not exist or has no addresses (permanent)
    DNS_TEMPORARY = "dns_temporary"  # temporary resolver failure; try again later
    DNS_ERROR = "dns_error"  # any other resolution failure
    CONNECT_ERROR = "connect_error"  # refused, reset, unreachable, disconnected
    TLS_ERROR = "tls_error"  # handshake or certificate failure
    TIMEOUT = "timeout"  # connect timeout or the overall fetch deadline
    TOO_MANY_REDIRECTS = "too_many_redirects"
    BAD_REDIRECT = "bad_redirect"  # 3xx without a usable Location
    RATE_LIMITED = "rate_limited"  # 429, or 503 with Retry-After: "slow down", not "dead"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    HTTP_OTHER = "http_other"  # any other non-2xx final status
    HEADER_TOO_LARGE = "header_too_large"
    PROTOCOL_ERROR = "protocol_error"  # malformed HTTP response or payload
    TOO_LARGE = "too_large"  # body over the cap, counted after decompression
    UNSUPPORTED_ENCODING = "unsupported_encoding"  # Content-Encoding we do not decode
    DECODE_ERROR = "decode_error"  # corrupt or truncated gzip/deflate stream
    NOT_A_FEED = "not_a_feed"  # parsed, but not RSS/Atom (HTML, other XML, JSON...)
    PARSE_ERROR = "parse_error"  # looks like XML but is malformed or uses DTD entities
    INTERNAL_ERROR = "internal_error"  # a bug in indexer; the run continues


class CheckError(Exception):
    """A failure that maps to one ErrorCode."""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else str(code))
        self.code = code
        self.detail = detail
