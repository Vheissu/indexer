"""The tables, as plain dataclasses: one field per column, named and typed like it (`X | None`
for a nullable column). Liquibase owns the schema (db/changelog); these only describe it, and a
database test checks that they match it. db.py builds its column lists from them."""

import ipaddress
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def generated() -> Any:
    """A column the database fills (identity, default): never written by the code."""
    return field(default=None, metadata={"generated": True})


def columns(model: "Model", *, writable: bool = False) -> tuple[str, ...]:
    """The model's column names; with `writable`, only those the code writes."""
    return tuple(f.name for f in fields(model) if not (writable and f.metadata.get("generated")))


def values(row: "Run | HealthCheck | Input | Feed") -> dict[str, object]:
    """A row's writable columns, as query parameters."""
    return {name: getattr(row, name) for name in columns(type(row), writable=True)}


@dataclass(frozen=True, kw_only=True)
class Run:
    """One row per invocation of `indexer run`; id is the run number."""

    TABLE: ClassVar = "runs"
    id: int = generated()
    started_at: datetime
    finished_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class HealthCheck:
    """One row per URL per run."""

    TABLE: ClassVar = "health_checks"
    id: int = generated()
    run_id: int
    checked_at: datetime
    url: str
    final_url: str | None = None
    http_status: int | None = None
    parent_domain: str | None = None
    last_item_date: datetime | None = None
    redirected: bool = False
    file_size: int | None = None
    item_count: int | None = None
    fetch_time_ms: int | None = None
    response_time_ms: int | None = None
    ttl: int | None = None
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    first_redirect_status: int | None = None
    ip_address: IPAddress | None = None


@dataclass(frozen=True, kw_only=True)
class Input:
    """A feed URL to check, from a list in data/sources.toml."""

    TABLE: ClassVar = "inputs"
    url: str
    source: str
    source_license: str
    first_seen_at: datetime = generated()
    last_checked_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class Feed:
    """Latest metadata per feed URL: replaced when a check parses the feed, kept when one fails."""

    TABLE: ClassVar = "feeds"
    url: str
    updated_at: datetime
    parsed_at: datetime | None = None
    feed_kind: str | None = None
    generator: str | None = None
    title: str | None = None
    subtitle: str | None = None
    language: str | None = None
    topics: str | None = None
    media_type: str | None = None
    icon: str | None = None
    logo: str | None = None
    rights: str | None = None
    license: str | None = None
    site_url: str | None = None
    self_url: str | None = None
    new_feed_url: str | None = None


type Model = type[Run] | type[HealthCheck] | type[Input] | type[Feed]
MODELS: tuple[Model, ...] = (Run, HealthCheck, Input, Feed)
