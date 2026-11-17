"""Fetch one untrusted URL with manual redirects, a deadline and a decompressed-size cap."""

import asyncio
import contextlib
import contextvars
import socket
import ssl
import time
import zlib
from collections.abc import AsyncIterator, Collection, Mapping
from dataclasses import dataclass, field

import aiohttp
from aiohappyeyeballs import AddrInfoType, SocketFactoryType
from aiohttp import hdrs
from yarl import URL

from indexer import __version__
from indexer.errors import CheckError, ErrorCode
from indexer.net.ippolicy import is_public_ip
from indexer.net.resolver import (
    BLOCKED_MARKER,
    BlockedAddressError,
    GuardedResolver,
    Lookup,
    guarded_socket_factory,
    system_lookup,
)
from indexer.net.validate import DEFAULT_ALLOWED_PORTS, IPPolicy, validate_url

USER_AGENT = f"indexer/{__version__} (awesome-rss feed health checker)"
ACCEPT = (
    "application/rss+xml, application/atom+xml, application/rdf+xml, "
    "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.1"
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
CHUNK_SIZE = 64 * 1024
MAX_VALIDATOR_LEN = 1024

# Addresses this fetch's current hop created sockets for. A ContextVar holding a list: each
# check runs in its own task, and tasks aiohttp starts for connecting copy the context, so they
# append to the same list.
_attempts: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "indexer_attempts", default=None
)


@dataclass(frozen=True, slots=True)
class FetchConfig:
    deadline: float = 15.0  # whole fetch: every hop, headers and body
    connect_timeout: float = 5.0
    max_body_bytes: int = 5 * 1024 * 1024  # after decompression
    max_redirects: int = 3
    max_line_size: int = 8190  # status line / single header line
    max_field_size: int = 8190  # single header value
    max_headers: int = 100
    concurrency: int = 20
    per_host: int = 2
    allowed_ports: Collection[int] = DEFAULT_ALLOWED_PORTS


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str | None = None
    http_status: int | None = None
    redirected: bool = False
    body: bytes | None = None
    fetch_time_ms: int | None = None
    response_time_ms: int | None = None
    error: ErrorCode | None = None
    detail: str | None = None
    hops: list[str] = field(default_factory=list)
    first_redirect_status: int | None = None
    # Final hop: the peer address of the response, or else the last address a socket was
    # created for (the connection that then failed).
    ip_address: str | None = None
    etag: str | None = None
    last_modified: str | None = None


def _validator(value: str | None) -> str | None:
    """A header value safe to store and send back: printable ASCII, bounded length."""
    if not value:
        return None
    value = value.strip()
    if not value or len(value) > MAX_VALIDATOR_LEN:
        return None
    if not all(0x20 <= ord(c) < 0x7F for c in value):
        return None
    return value


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class HostLimiter:
    """At most `per_host` concurrent requests per hostname."""

    def __init__(self, per_host: int) -> None:
        self._per_host = per_host
        self._sems: dict[str, asyncio.Semaphore] = {}

    def get(self, host: str) -> asyncio.Semaphore:
        key = host.lower().rstrip(".")
        sem = self._sems.get(key)
        if sem is None:
            sem = self._sems[key] = asyncio.Semaphore(self._per_host)
        return sem


class _HostSlot:
    """Holds one host's slot; moving to another host releases the old slot first."""

    def __init__(self, limiter: HostLimiter) -> None:
        self._limiter = limiter
        self._sem: asyncio.Semaphore | None = None

    async def move_to(self, host: str) -> None:
        sem = self._limiter.get(host)
        if sem is self._sem:
            return
        self.release()
        await sem.acquire()
        self._sem = sem

    def release(self) -> None:
        if self._sem is not None:
            self._sem.release()
            self._sem = None


def _content_encoding(value: str | None) -> str | None:
    """Return 'gzip', 'deflate' or None (identity); raise for anything else."""
    codings = [c.strip().lower() for c in (value or "").split(",")]
    codings = [c for c in codings if c and c != "identity"]
    if not codings:
        return None
    if len(codings) == 1 and codings[0] in ("gzip", "x-gzip"):
        return "gzip"
    if len(codings) == 1 and codings[0] == "deflate":
        return "deflate"
    raise CheckError(ErrorCode.UNSUPPORTED_ENCODING, value or "")


class _Inflater:
    """Incremental gzip/deflate decoding that never produces more than the cap allows."""

    def __init__(self, encoding: str) -> None:
        self._encoding = encoding
        # 47 = auto-detect a gzip or zlib header. Raw deflate is tried if zlib fails at once.
        self._d = zlib.decompressobj(wbits=47)
        self._started = False

    def feed(self, chunk: bytes, remaining: int) -> bytes:
        try:
            data = self._d.decompress(chunk, remaining + 1)
        except zlib.error as exc:
            if self._encoding == "deflate" and not self._started:
                self._d = zlib.decompressobj(wbits=-15)
                self._started = True
                return self.feed(chunk, remaining)
            raise CheckError(ErrorCode.DECODE_ERROR, str(exc)) from exc
        self._started = True
        if len(data) > remaining or self._d.unconsumed_tail:
            raise CheckError(ErrorCode.TOO_LARGE, "decompressed body over the size limit")
        return data

    def finish(self, remaining: int) -> bytes:
        try:
            data = self._d.flush(remaining + 1)
        except zlib.error as exc:
            raise CheckError(ErrorCode.DECODE_ERROR, str(exc)) from exc
        if len(data) > remaining:
            raise CheckError(ErrorCode.TOO_LARGE, "decompressed body over the size limit")
        if not self._d.eof:
            raise CheckError(ErrorCode.DECODE_ERROR, "truncated compressed body")
        return data


async def read_body(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    encoding = _content_encoding(resp.headers.get(hdrs.CONTENT_ENCODING))
    if resp.content_length is not None and resp.content_length > max_bytes:
        # The compressed size alone already exceeds the cap; the decompressed size can only be
        # larger (or the server is lying), so stop before reading.
        raise CheckError(ErrorCode.TOO_LARGE, f"Content-Length {resp.content_length}")
    inflater = _Inflater(encoding) if encoding else None
    out = bytearray()
    raw_total = 0
    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
        raw_total += len(chunk)
        if raw_total > max_bytes:
            raise CheckError(ErrorCode.TOO_LARGE, "body over the size limit")
        if inflater is None:
            out += chunk
        else:
            out += inflater.feed(chunk, max_bytes - len(out))
        if len(out) > max_bytes:
            raise CheckError(ErrorCode.TOO_LARGE, "body over the size limit")
    if inflater is not None:
        out += inflater.finish(max_bytes - len(out))
    return bytes(out)


def _iter_chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack and len(seen) < 20:
        cur = stack.pop()
        if any(cur is s for s in seen):
            continue
        seen.append(cur)
        for nxt in (cur.__cause__, cur.__context__, getattr(cur, "os_error", None)):
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
    return seen


_DNS_NOT_FOUND = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}


def _dns_code(errno: int | None) -> ErrorCode:
    if errno in _DNS_NOT_FOUND:
        return ErrorCode.DNS_NXDOMAIN
    if errno == socket.EAI_AGAIN:
        return ErrorCode.DNS_TEMPORARY
    return ErrorCode.DNS_ERROR


def classify_exception(exc: BaseException) -> tuple[ErrorCode, str]:
    """Map an exception raised while fetching to an ErrorCode and a short detail."""
    chain = _iter_chain(exc)
    detail = f"{type(exc).__name__}: {exc}"[:500]
    blocked = next(
        (e for e in chain if isinstance(e, BlockedAddressError) or BLOCKED_MARKER in str(e)),
        None,
    )
    if blocked is not None:
        # Our own message names the host and the refused addresses; aiohttp's wrapper
        # ("Cannot connect to host ... [None]") does not.
        return ErrorCode.BLOCKED_IP, str(blocked).replace(BLOCKED_MARKER, "").strip()[:500]
    if isinstance(exc, CheckError):
        return exc.code, exc.detail
    gai = next((e for e in chain if isinstance(e, socket.gaierror)), None)
    if gai is not None:
        return _dns_code(gai.errno), detail
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return ErrorCode.DNS_ERROR, detail
    if isinstance(exc, (aiohttp.ClientSSLError, ssl.SSLError)) or any(
        isinstance(e, ssl.SSLError) for e in chain
    ):
        return ErrorCode.TLS_ERROR, detail
    if isinstance(exc, (TimeoutError, aiohttp.ServerTimeoutError)):
        return ErrorCode.TIMEOUT, detail
    if isinstance(exc, aiohttp.InvalidURL):
        return ErrorCode.INVALID_URL, detail
    if isinstance(exc, aiohttp.ClientResponseError):
        text = str(exc).lower()
        if any(s in text for s in ("got more than", "too long", "too many headers", "too large")):
            return ErrorCode.HEADER_TOO_LARGE, detail
        return ErrorCode.PROTOCOL_ERROR, detail
    if isinstance(exc, aiohttp.ClientPayloadError):
        return ErrorCode.PROTOCOL_ERROR, detail
    if isinstance(exc, (aiohttp.ClientConnectionError, OSError)):
        return ErrorCode.CONNECT_ERROR, detail
    if isinstance(exc, aiohttp.ClientError):
        return ErrorCode.PROTOCOL_ERROR, detail
    return ErrorCode.INTERNAL_ERROR, detail


def create_session(
    config: FetchConfig,
    *,
    resolver: aiohttp.abc.AbstractResolver,
    socket_factory: SocketFactoryType,
) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,  # every new connection is resolved (and checked) again
        socket_factory=socket_factory,
        limit=config.concurrency,
        limit_per_host=config.per_host,
        ssl=ssl.create_default_context(),
    )
    return aiohttp.ClientSession(
        connector=connector,
        cookie_jar=aiohttp.DummyCookieJar(),  # never store or send cookies
        trust_env=False,  # ignore HTTP(S)_PROXY and .netrc
        auto_decompress=False,  # we decompress ourselves, with a size cap
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=config.connect_timeout),
        headers={
            hdrs.USER_AGENT: USER_AGENT,
            hdrs.ACCEPT: ACCEPT,
            hdrs.ACCEPT_ENCODING: "gzip, deflate",
        },
        max_line_size=config.max_line_size,
        max_field_size=config.max_field_size,
        max_headers=config.max_headers,
    )


def _peer_address(resp: aiohttp.ClientResponse) -> str | None:
    conn = resp.connection
    transport = conn.transport if conn is not None else None
    peer = transport.get_extra_info("peername") if transport is not None else None
    return str(peer[0]) if peer else None


def _recording(factory: SocketFactoryType) -> SocketFactoryType:
    """Note each address a socket is created for, so a failed connection can be reported."""

    def wrapped(addr_info: AddrInfoType) -> socket.socket:
        sock = factory(addr_info)
        if (attempts := _attempts.get()) is not None:
            attempts.append(str(addr_info[4][0]))
        return sock

    return wrapped


class Fetcher:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: FetchConfig,
        policy: IPPolicy = is_public_ip,
    ) -> None:
        self._session = session
        self._config = config
        self._policy = policy
        self._global = asyncio.Semaphore(config.concurrency)
        self._hosts = HostLimiter(config.per_host)

    def _validate(self, url: str | URL) -> URL:
        return validate_url(url, allowed_ports=self._config.allowed_ports, policy=self._policy)

    async def fetch(self, raw_url: str, headers: Mapping[str, str] | None = None) -> FetchResult:
        """Never raises (except on cancellation): failures are reported in result.error.

        `headers` are extra request headers (e.g. Range). They are sent to the original host
        only: a redirect to any other host drops them."""
        result = FetchResult(url=raw_url)
        try:
            url = self._validate(raw_url)
        except CheckError as exc:
            result.error, result.detail = exc.code, exc.detail
            return result

        slot = _HostSlot(self._hosts)
        attempts: list[str] = []
        token = _attempts.set(attempts)
        try:
            # Queueing for a slot does not count against the deadline; the clock starts
            # once this fetch may actually send its first request.
            await slot.move_to(url.host or "")
            async with self._global:
                start = time.monotonic()
                deadline = asyncio.timeout(self._config.deadline)
                try:
                    async with deadline:
                        await self._follow(url, result, slot, headers)
                except TimeoutError as exc:
                    # aiohttp's connect timeout is also a TimeoutError; tell them apart.
                    result.error = ErrorCode.TIMEOUT
                    result.detail = (
                        "fetch deadline exceeded"
                        if deadline.expired()
                        else f"{type(exc).__name__}: {exc}"[:500]
                    )
                except Exception as exc:  # noqa: BLE001 - every failure becomes a row
                    result.error, result.detail = classify_exception(exc)
                finally:
                    result.fetch_time_ms = _ms(time.monotonic() - start)
                if result.ip_address is None and attempts:
                    result.ip_address = attempts[-1]
        finally:
            _attempts.reset(token)
            slot.release()
        if result.error is not None:
            result.body = None
        return result

    async def _follow(
        self,
        url: URL,
        result: FetchResult,
        slot: _HostSlot,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        origin = (url.host or "").lower()
        for hop in range(self._config.max_redirects + 1):
            if hop > 0:
                url = self._validate(url)  # scheme, credentials, port, IP literal
                await slot.move_to(url.host or "")
            result.final_url = str(url)
            result.hops.append(str(url))
            result.ip_address = None
            if (attempts := _attempts.get()) is not None:
                attempts.clear()
            request_start = time.monotonic()
            hop_headers = headers if (url.host or "").lower() == origin else None
            async with self._session.get(url, allow_redirects=False, headers=hop_headers) as resp:
                result.response_time_ms = _ms(time.monotonic() - request_start)
                result.http_status = resp.status
                result.ip_address = _peer_address(resp)
                if resp.status in REDIRECT_STATUSES:
                    if result.first_redirect_status is None:
                        result.first_redirect_status = resp.status
                    location = resp.headers.get(hdrs.LOCATION)
                    if not location:
                        raise CheckError(ErrorCode.BAD_REDIRECT, f"{resp.status} without Location")
                    try:
                        next_url = url.join(URL(location))
                    except (ValueError, TypeError) as exc:
                        raise CheckError(ErrorCode.BAD_REDIRECT, f"bad Location: {exc}") from exc
                    result.redirected = True
                    if hop == self._config.max_redirects:
                        raise CheckError(
                            ErrorCode.TOO_MANY_REDIRECTS,
                            f"more than {self._config.max_redirects} redirects",
                        )
                    url = next_url
                    continue
                retry_after = resp.headers.get(hdrs.RETRY_AFTER)
                if resp.status == 429 or (resp.status == 503 and retry_after):
                    detail = f"{resp.status}, Retry-After: {retry_after}"
                    raise CheckError(ErrorCode.RATE_LIMITED, detail[:200])
                if 400 <= resp.status < 500:
                    raise CheckError(ErrorCode.HTTP_4XX, str(resp.status))
                if 500 <= resp.status < 600:
                    raise CheckError(ErrorCode.HTTP_5XX, str(resp.status))
                if not 200 <= resp.status < 300:
                    raise CheckError(ErrorCode.HTTP_OTHER, str(resp.status))
                result.body = await read_body(resp, self._config.max_body_bytes)
                result.etag = _validator(resp.headers.get(hdrs.ETAG))
                result.last_modified = _validator(resp.headers.get(hdrs.LAST_MODIFIED))
                return


@contextlib.asynccontextmanager
async def open_fetcher(
    config: FetchConfig | None = None,
    *,
    lookup: Lookup = system_lookup,
    policy: IPPolicy = is_public_ip,
    socket_factory: SocketFactoryType | None = None,
) -> AsyncIterator[Fetcher]:
    config = config or FetchConfig()
    resolver = GuardedResolver(lookup=lookup, policy=policy)
    factory = _recording(socket_factory or guarded_socket_factory(policy))
    async with create_session(config, resolver=resolver, socket_factory=factory) as session:
        yield Fetcher(session, config, policy)
