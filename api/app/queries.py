"""Read side: queries that build API responses (writes go through services)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFound
from app.models import (
    COPY_ROW,
    AppUser,
    Copy,
    Node,
    NodeOverview,
    NodeStatus,
    Rental,
    RentalStatus,
    Title,
    TitleAvailability,
    Violation,
)
from app.schemas import (
    CopyOut,
    NodeOut,
    RentalOut,
    StatsOut,
    TitleDetailOut,
    TitleOut,
    ViolationOut,
)

_copy_row = COPY_ROW  # whole-row reference for the SQL methods: is_available(copy)


# -------------------------------------------------------------------- titles


def _title_out(t: TitleAvailability) -> TitleOut:
    imdb = t.providers.imdb if t.providers else None
    return TitleOut(
        id=t.title_id,
        name=t.name,
        year=t.year,
        genres=t.genres or [],
        rating=t.rating,
        imdb_url=f"https://www.imdb.com/title/{imdb}/" if imdb else None,
        available=t.available,
        total=t.total,
        nodes=t.nodes or [],
        available_nodes=t.available_nodes or [],
    )


async def list_titles(
    session: AsyncSession, *, q: str | None, genre: str | None, available_only: bool
) -> list[TitleOut]:
    stmt = select(TitleAvailability).order_by(TitleAvailability.name, TitleAvailability.year)
    if q:
        stmt = stmt.where(TitleAvailability.name.icontains(q, autoescape=True))
    if genre:
        stmt = stmt.where(TitleAvailability.genres.any_() == genre)
    if available_only:
        stmt = stmt.where(TitleAvailability.available > 0)
    return [_title_out(t) for t in (await session.scalars(stmt)).all()]


async def get_title(session: AsyncSession, title_id: int) -> TitleDetailOut:
    availability = await session.get(TitleAvailability, title_id)
    if availability is None:
        raise NotFound(f"title {title_id}")
    rows = await session.execute(
        select(
            Copy.id,
            Copy.node_id,
            Node.name,
            Node.status,
            Copy.media,
            func.is_available(_copy_row).label("available"),
            func.resolution(Copy.media).label("resolution"),
        )
        .join(Node, Node.id == Copy.node_id)
        .where(Copy.title_id == title_id, Copy.is_present)
        .order_by(Copy.node_id, Copy.id)
    )
    copies = [
        CopyOut(
            id=r.id,
            node_id=r.node_id,
            node=r.name,
            node_status=r.status,
            available=bool(r.available),
            resolution=r.resolution,
            container=r.media.container if r.media else None,
            video_codec=r.media.video_codec if r.media else None,
            runtime_sec=r.media.runtime_sec if r.media else None,
        )
        for r in rows
    ]
    return TitleDetailOut(**_title_out(availability).model_dump(), copies=copies)


# ------------------------------------------------------------------- rentals


def _rental_query() -> Select[Any]:
    return (
        select(
            Rental,
            Title.id.label("title_id"),
            Title.name.label("title_name"),
            Node.id.label("node_id"),
            Node.name.label("node_name"),
            AppUser.username,
            func.play_url(_copy_row).label("play_url"),
        )
        .join(Copy, Copy.id == Rental.copy_id)
        .join(Title, Title.id == Copy.title_id)
        .join(Node, Node.id == Copy.node_id)
        .join(AppUser, AppUser.id == Rental.user_id)
    )


def _rental_out(row: Any) -> RentalOut:
    rental: Rental = row.Rental
    granted = RentalStatus(rental.status).grants_access
    return RentalOut(
        id=rental.id,
        status=rental.status,
        return_reason=rental.return_reason,
        title_id=row.title_id,
        title=row.title_name,
        node_id=row.node_id,
        node=row.node_name,
        username=row.username,
        rented_at=rental.rented_at,
        due_at=rental.due_at,
        returned_at=rental.returned_at,
        play_url=row.play_url if granted else None,
    )


async def rental_view(session: AsyncSession, rental_id: int) -> RentalOut:
    row = (await session.execute(_rental_query().where(Rental.id == rental_id))).one_or_none()
    if row is None:
        raise NotFound(f"rental {rental_id}")
    return _rental_out(row)


async def list_rentals(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    statuses: list[RentalStatus] | None = None,
    limit: int = 200,
) -> list[RentalOut]:
    stmt = _rental_query().order_by(Rental.rented_at.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(Rental.user_id == user_id)
    if statuses:
        stmt = stmt.where(Rental.status.in_(statuses))
    return [_rental_out(row) for row in await session.execute(stmt)]


# --------------------------------------------------------------------- admin


async def list_nodes(session: AsyncSession) -> list[NodeOut]:
    nodes = (await session.scalars(select(NodeOverview).order_by(NodeOverview.id))).all()
    return [NodeOut.model_validate(n) for n in nodes]


async def list_violations(session: AsyncSession, limit: int = 100) -> list[ViolationOut]:
    rows = await session.execute(
        select(
            Violation,
            Node.name.label("node_name"),
            Title.name.label("title_name"),
            AppUser.username,
        )
        .join(Node, Node.id == Violation.node_id)
        .join(Copy, Copy.id == Violation.copy_id)
        .join(Title, Title.id == Copy.title_id)
        .outerjoin(AppUser, AppUser.id == Violation.user_id)
        .order_by(Violation.detected_at.desc())
        .limit(limit)
    )
    return [
        ViolationOut(
            id=r.Violation.id,
            node=r.node_name,
            copy_id=r.Violation.copy_id,
            title=r.title_name,
            username=r.username,
            jf_session_id=r.Violation.jf_session_id,
            reason=r.Violation.reason,
            detail=r.Violation.detail,
            detected_at=r.Violation.detected_at,
        )
        for r in rows
    ]


async def stats(session: AsyncSession) -> StatsOut:
    live = [RentalStatus.PENDING, RentalStatus.ACTIVE, RentalStatus.RETURNING]
    row = (
        await session.execute(
            select(
                select(func.count()).select_from(Title).scalar_subquery().label("titles"),
                select(func.count())
                .select_from(Copy)
                .where(Copy.is_present)
                .scalar_subquery()
                .label("copies"),
                select(func.count())
                .select_from(Node)
                .where(Node.status == NodeStatus.ONLINE)
                .scalar_subquery()
                .label("nodes_online"),
                select(func.count()).select_from(Node).scalar_subquery().label("nodes_total"),
                select(func.count())
                .select_from(Rental)
                .where(Rental.status.in_(live))
                .scalar_subquery()
                .label("live_rentals"),
                select(func.count()).select_from(Rental).scalar_subquery().label("rentals_total"),
                select(func.count())
                .select_from(Violation)
                .where(Violation.detected_at > func.now() - timedelta(days=1))
                .scalar_subquery()
                .label("violations_24h"),
            )
        )
    ).one()
    return StatsOut(**row._mapping)
