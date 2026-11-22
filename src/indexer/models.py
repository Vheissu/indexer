"""The tables, as Python classes. Liquibase owns the schema (db/changelog): these mirror it and
never create or alter anything; keep them in sync with the changesets."""

import ipaddress
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import BigInteger, DateTime, ForeignKey, SmallInteger
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        IPAddress: INET,
    }


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("runs.id"))
    checked_at: Mapped[datetime]
    url: Mapped[str]
    final_url: Mapped[str | None]
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    parent_domain: Mapped[str | None]
    last_item_date: Mapped[datetime | None]
    redirected: Mapped[bool]
    file_size: Mapped[int | None]
    item_count: Mapped[int | None]
    fetch_time_ms: Mapped[int | None]
    response_time_ms: Mapped[int | None]
    ttl: Mapped[int | None]
    error: Mapped[str | None]
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    first_redirect_status: Mapped[int | None] = mapped_column(SmallInteger)
    ip_address: Mapped[IPAddress | None]


class Input(Base):
    __tablename__ = "inputs"

    url: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[str]
    source_license: Mapped[str]
    first_seen_at: Mapped[datetime]
    last_checked_at: Mapped[datetime | None]


class Feed(Base):
    __tablename__ = "feeds"

    url: Mapped[str] = mapped_column(primary_key=True)
    updated_at: Mapped[datetime]
    parsed_at: Mapped[datetime | None]
    feed_kind: Mapped[str | None]
    generator: Mapped[str | None]
    title: Mapped[str | None]
    subtitle: Mapped[str | None]
    language: Mapped[str | None]
    topics: Mapped[str | None]
    media_type: Mapped[str | None]
    icon: Mapped[str | None]
    logo: Mapped[str | None]
    rights: Mapped[str | None]
    license: Mapped[str | None]
    site_url: Mapped[str | None]
    self_url: Mapped[str | None]
    new_feed_url: Mapped[str | None]
