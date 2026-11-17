"""Check every URL concurrently. Each URL yields exactly one CheckRow, whatever happens."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from indexer.check.feed import parse_feed
from indexer.errors import CheckError, ErrorCode
from indexer.net.domains import parent_domain_of
from indexer.net.fetch import FetchResult

log = logging.getLogger(__name__)


class SupportsFetch(Protocol):
    async def fetch(self, raw_url: str) -> FetchResult: ...


@dataclass(slots=True)
class CheckRow:
    url: str
    checked_at: datetime
    final_url: str | None = None
    http_status: int | None = None
    parent_domain: str | None = None
    last_item_date: datetime | None = None
    redirected: bool = False
    first_redirect_status: int | None = None
    ip_address: str | None = None
    file_size: int | None = None
    item_count: int | None = None
    fetch_time_ms: int | None = None
    response_time_ms: int | None = None
    ttl: int | None = None
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    feed_kind: str | None = None
    generator: str | None = None
    site_url: str | None = None
    self_url: str | None = None
    new_feed_url: str | None = None
    title: str | None = None
    subtitle: str | None = None
    categories: str | None = None
    item_categories: str | None = None
    itunes_category: str | None = None
    language: str | None = None
    media_type: str | None = None
    icon: str | None = None
    logo: str | None = None
    rights: str | None = None
    license: str | None = None
    last_tagged_item_date: datetime | None = None  # used by survivors, not stored
    item_links: tuple[str, ...] = ()  # used by survivors, not stored
    detail: str | None = None  # logged, not stored


async def check_one(fetcher: SupportsFetch, url: str) -> CheckRow:
    row = CheckRow(url=url, checked_at=datetime.now(UTC))
    try:
        row.parent_domain = parent_domain_of(url)
        res = await fetcher.fetch(url)
        row.final_url = res.final_url
        row.http_status = res.http_status
        row.redirected = res.redirected
        row.first_redirect_status = res.first_redirect_status
        row.ip_address = res.ip_address
        row.fetch_time_ms = res.fetch_time_ms
        row.response_time_ms = res.response_time_ms
        row.etag, row.last_modified = res.etag, res.last_modified
        if res.error is not None:
            row.error, row.detail = str(res.error), res.detail
            return row
        body = res.body or b""
        row.file_size = len(body)
        try:
            info = await asyncio.to_thread(parse_feed, body, res.final_url)
        except CheckError as exc:
            row.error, row.detail = str(exc.code), exc.detail
            return row
        row.item_count = info.item_count
        row.last_item_date = info.last_item_date
        row.last_tagged_item_date = info.last_tagged_item_date
        row.ttl = info.ttl
        row.feed_kind = info.kind
        row.generator = info.generator
        row.site_url = info.site_url
        row.self_url = info.self_url
        row.new_feed_url = info.new_feed_url
        row.title = info.title
        row.subtitle = info.subtitle
        row.categories = info.categories
        row.item_categories = info.item_categories
        row.item_links = info.item_links
        row.itunes_category = info.itunes_category
        row.language = info.language
        row.media_type = info.media_type
        row.icon = info.icon
        row.logo = info.logo
        row.rights = info.rights
        row.license = info.license
    except Exception as exc:
        log.exception("internal error checking %s", url)
        row.error, row.detail = str(ErrorCode.INTERNAL_ERROR), f"{type(exc).__name__}: {exc}"
    return row


async def run_checks(
    urls: Sequence[str],
    fetcher: SupportsFetch,
    on_row: Callable[[CheckRow], Awaitable[None]] | None = None,
) -> list[CheckRow]:
    async def one(url: str) -> CheckRow:
        row = await check_one(fetcher, url)
        if on_row is not None:
            await on_row(row)
        return row

    return list(await asyncio.gather(*(one(u) for u in urls)))
