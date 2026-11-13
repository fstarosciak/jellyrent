"""Object mapping of the central database (SQLAlchemy 2.0 declarative).

The schema is owned by SQL (db/init/*.sql); these classes map onto it and
never create it. The object-relational features map as follows:

* ENUM types            -> Python StrEnum + postgresql.ENUM(create_type=False)
* composite types       -> frozen dataclasses via PgComposite (app.db_types)
* arrays                -> list[str] via ARRAY(Text)
* table inheritance     -> abstract Python base class MediaItem mirrors the
                           abstract parent table media_item; Title maps the
                           concrete child table title
* methods on row types  -> deferred column properties calling the SQL
                           functions (Copy.is_available, Copy.play_url, ...)
* views                 -> read-only mapped classes (TitleAvailability, ...)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from sqlalchemy import BigInteger, ForeignKey, Integer, Table, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    deferred,
    mapped_column,
    relationship,
)
from sqlalchemy.types import DateTime

from app.db_types import PgComposite

# --------------------------------------------------------------------------- enums


class NodeStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    MAINTENANCE = "MAINTENANCE"


class RentalStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    RETURNING = "RETURNING"
    RETURNED = "RETURNED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"

    @property
    def is_live(self) -> bool:
        """Live rentals hold the copy (see the partial unique index)."""
        return self in LIVE_RENTAL_STATUSES

    @property
    def grants_access(self) -> bool:
        """Rentals whose copy tag belongs in the user's AllowedTags."""
        return self in ACCESS_GRANTING_STATUSES


LIVE_RENTAL_STATUSES = frozenset(
    {RentalStatus.PENDING, RentalStatus.ACTIVE, RentalStatus.RETURNING}
)
ACCESS_GRANTING_STATUSES = frozenset({RentalStatus.PENDING, RentalStatus.ACTIVE})


class ReturnReason(StrEnum):
    USER = "USER"
    EXPIRED = "EXPIRED"
    ADMIN = "ADMIN"


class ViolationReason(StrEnum):
    NO_RENTAL = "NO_RENTAL"
    CONCURRENT_SESSION = "CONCURRENT_SESSION"


def _pg_enum(enum_cls: type[StrEnum], name: str) -> ENUM:
    return ENUM(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=lambda members: [m.value for m in members],
    )


# ------------------------------------------------------------------ composite types


@dataclass(frozen=True, slots=True)
class ProviderIds:
    imdb: str | None = None
    tmdb: str | None = None


@dataclass(frozen=True, slots=True)
class MediaInfo:
    container: str | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    runtime_sec: int | None = None


# --------------------------------------------------------------------------- base


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy convention)
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
    }


# ------------------------------------------------------------------ media hierarchy


class MediaItem(Base):
    """Abstract base mirroring the abstract parent table ``media_item``."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    year: Mapped[int | None]
    providers: Mapped[ProviderIds | None] = mapped_column(PgComposite("provider_ids", ProviderIds))
    genres: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Title(MediaItem):
    """A film as a work (table ``title INHERITS (media_item)``)."""

    __tablename__ = "title"

    rating: Mapped[Decimal | None]

    copies: Mapped[list[Copy]] = relationship(back_populates="title")


# ------------------------------------------------------------------------ topology


class Node(Base):
    __tablename__ = "node"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    base_url: Mapped[str] = mapped_column(Text)
    public_url: Mapped[str] = mapped_column(Text)
    sqlite_path: Mapped[str] = mapped_column(Text)
    api_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[NodeStatus] = mapped_column(_pg_enum(NodeStatus, "node_status"))
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    last_seen_at: Mapped[datetime | None]
    last_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)

    copies: Mapped[list[Copy]] = relationship(back_populates="node")


# -------------------------------------------------------------------------- copies


class Copy(Base):
    """A physical copy ("disc"): one media file on one node."""

    __tablename__ = "copy"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id"))
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"))
    jf_item_id: Mapped[uuid.UUID]
    path: Mapped[str | None] = mapped_column(Text)
    media: Mapped[MediaInfo | None] = mapped_column(PgComposite("media_info", MediaInfo))
    is_present: Mapped[bool] = mapped_column(default=True)
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())

    title: Mapped[Title] = relationship(back_populates="copies")
    node: Mapped[Node] = relationship(back_populates="copies")


# Whole-row reference to `copy`, rendered as the bare table name, e.g.
# is_available(copy). Valid wherever the copy table is not aliased.
COPY_ROW = cast(Table, Copy.__table__).table_valued()

# Methods defined on the row type in SQL, exposed as lazily loaded attributes.
Copy.is_available = deferred(func.is_available(COPY_ROW))
Copy.play_url = deferred(func.play_url(COPY_ROW))
Copy.access_tag = deferred(func.access_tag(COPY_ROW))


# --------------------------------------------------------------------------- users


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(default=False)
    max_rentals: Mapped[int] = mapped_column(default=3)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    accounts: Mapped[list[NodeAccount]] = relationship(back_populates="user")


class NodeAccount(Base):
    """The user's account on one Jellyfin node."""

    __tablename__ = "node_account"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"), primary_key=True)
    jf_user_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user: Mapped[AppUser] = relationship(back_populates="accounts")
    node: Mapped[Node] = relationship()


# ------------------------------------------------------------------------- rentals


class Rental(Base):
    __tablename__ = "rental"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    copy_id: Mapped[int] = mapped_column(ForeignKey("copy.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    status: Mapped[RentalStatus] = mapped_column(_pg_enum(RentalStatus, "rental_status"))
    return_reason: Mapped[ReturnReason | None] = mapped_column(
        _pg_enum(ReturnReason, "return_reason")
    )
    rented_at: Mapped[datetime] = mapped_column(server_default=func.now())
    due_at: Mapped[datetime]
    activated_at: Mapped[datetime | None]
    returned_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    copy: Mapped[Copy] = relationship()
    user: Mapped[AppUser] = relationship()
    events: Mapped[list[RentalEvent]] = relationship(
        back_populates="rental", order_by="RentalEvent.occurred_at"
    )


class RentalEvent(Base):
    """Saga log entry, written by the rental_log trigger."""

    __tablename__ = "rental_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rental_id: Mapped[int] = mapped_column(ForeignKey("rental.id"))
    from_status: Mapped[RentalStatus | None] = mapped_column(
        _pg_enum(RentalStatus, "rental_status")
    )
    to_status: Mapped[RentalStatus] = mapped_column(_pg_enum(RentalStatus, "rental_status"))
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())

    rental: Mapped[Rental] = relationship(back_populates="events")


# --------------------------------------------------------------------- session guard


class PlaybackSession(Base):
    __tablename__ = "playback_session"

    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"), primary_key=True)
    jf_session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    copy_id: Mapped[int] = mapped_column(ForeignKey("copy.id"), primary_key=True)
    jf_user_id: Mapped[uuid.UUID | None]
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Violation(Base):
    __tablename__ = "violation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"))
    copy_id: Mapped[int] = mapped_column(ForeignKey("copy.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    jf_user_id: Mapped[uuid.UUID | None]
    jf_session_id: Mapped[str] = mapped_column(Text)
    reason: Mapped[ViolationReason] = mapped_column(_pg_enum(ViolationReason, "violation_reason"))
    detail: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())

    copy: Mapped[Copy] = relationship()
    node: Mapped[Node] = relationship()
    user: Mapped[AppUser | None] = relationship()


# --------------------------------------------------------------------------- views


class TitleAvailability(Base):
    """Read model over the ``title_availability`` view."""

    __tablename__ = "title_availability"
    __table_args__ = {"info": {"is_view": True}}  # noqa: RUF012

    title_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    year: Mapped[int | None]
    genres: Mapped[list[str]] = mapped_column(ARRAY(Text))
    rating: Mapped[Decimal | None]
    providers: Mapped[ProviderIds | None] = mapped_column(PgComposite("provider_ids", ProviderIds))
    total: Mapped[int]
    available: Mapped[int]
    nodes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    available_nodes: Mapped[list[str]] = mapped_column(ARRAY(Text))


class NodeOverview(Base):
    """Read model over the ``node_overview`` view."""

    __tablename__ = "node_overview"
    __table_args__ = {"info": {"is_view": True}}  # noqa: RUF012

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[NodeStatus] = mapped_column(_pg_enum(NodeStatus, "node_status"))
    public_url: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None]
    last_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    provisioned: Mapped[bool]
    copies: Mapped[int]
    live_rentals: Mapped[int]
