"""Renting and returning (user side)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app import queries
from app.deps import CurrentUser, ServicesDep, SessionDep
from app.errors import NotFound
from app.models import LIVE_RENTAL_STATUSES, RentalStatus, ReturnReason
from app.schemas import RentalOut, RentIn

router = APIRouter(prefix="/rentals", tags=["rentals"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Rental limit, already rented or no free copy"},
        502: {"description": "The node could not grant access; the rental was cancelled"},
    },
)
async def rent(
    body: RentIn, user: CurrentUser, services: ServicesDep, session: SessionDep
) -> RentalOut:
    """Rent a title. The broker picks a free copy (or the pinned node's copy)."""
    record = await services.saga.rent(user.id, body.title_id, body.node_id, body.duration_hours)
    return await queries.rental_view(session, record.id)


@router.get("/me")
async def my_rentals(
    user: CurrentUser, session: SessionDep, include_closed: bool = False
) -> list[RentalOut]:
    statuses = None if include_closed else sorted(LIVE_RENTAL_STATUSES)
    return await queries.list_rentals(session, user_id=user.id, statuses=statuses)


@router.post(
    "/{rental_id}/return",
    responses={202: {"description": "Return accepted; the node is unreachable, retrying"}},
)
async def return_rental(
    rental_id: int,
    user: CurrentUser,
    services: ServicesDep,
    session: SessionDep,
    response: Response,
) -> RentalOut:
    record = await services.rental_store.get(rental_id)
    if record is None or record.user_id != user.id:
        raise NotFound(f"rental {rental_id}")  # do not reveal other users' rentals
    result = await services.saga.return_rental(rental_id, ReturnReason.USER)
    if result.status is RentalStatus.RETURNING:
        response.status_code = status.HTTP_202_ACCEPTED
    return await queries.rental_view(session, rental_id)
