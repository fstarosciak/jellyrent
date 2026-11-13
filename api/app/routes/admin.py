"""Administration panel endpoints (admin role required)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import update

from app import queries
from app.deps import AdminUser, ServicesDep, SessionDep
from app.errors import NotFound
from app.models import Node, NodeStatus, RentalStatus, ReturnReason
from app.schemas import (
    NodeOut,
    NodePatch,
    NodeSyncOut,
    RentalOut,
    StatsOut,
    SyncOut,
    ViolationOut,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/nodes")
async def nodes(_: AdminUser, session: SessionDep) -> list[NodeOut]:
    return await queries.list_nodes(session)


@router.patch("/nodes/{node_id}")
async def patch_node(
    node_id: int, body: NodePatch, _: AdminUser, session: SessionDep
) -> list[NodeOut]:
    """Enter or leave maintenance.

    Leaving maintenance sets the node OFFLINE: the heartbeat then reconciles
    its accounts and promotes it to ONLINE, like any recovering node.
    """
    values: dict[str, object] = (
        {"status": NodeStatus.MAINTENANCE}
        if body.maintenance
        else {"status": NodeStatus.OFFLINE, "consecutive_failures": 0}
    )
    result = await session.execute(
        update(Node).where(Node.id == node_id).values(**values).returning(Node.id)
    )
    if result.scalar_one_or_none() is None:
        raise NotFound(f"node {node_id}")
    await session.commit()
    return await queries.list_nodes(session)


@router.post("/sync")
async def sync(_: AdminUser, services: ServicesDep) -> SyncOut:
    """Run the catalog sync now (409 if one is already running)."""
    report = await services.catalog.run()
    return SyncOut(
        ok=report.ok,
        nodes=[NodeSyncOut.model_validate(n, from_attributes=True) for n in report.nodes],
    )


@router.get("/rentals")
async def rentals(
    _: AdminUser,
    session: SessionDep,
    status_filter: Annotated[list[RentalStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[RentalOut]:
    return await queries.list_rentals(session, statuses=status_filter, limit=limit)


@router.post("/rentals/{rental_id}/return")
async def force_return(
    rental_id: int, _: AdminUser, services: ServicesDep, session: SessionDep, response: Response
) -> RentalOut:
    result = await services.saga.return_rental(rental_id, ReturnReason.ADMIN)
    if result.status is RentalStatus.RETURNING:
        response.status_code = status.HTTP_202_ACCEPTED
    return await queries.rental_view(session, rental_id)


@router.get("/violations")
async def violations(
    _: AdminUser, session: SessionDep, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> list[ViolationOut]:
    return await queries.list_violations(session, limit=limit)


@router.get("/stats")
async def stats(_: AdminUser, session: SessionDep) -> StatsOut:
    return await queries.stats(session)
