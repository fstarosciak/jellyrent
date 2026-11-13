"""The saga orchestrator against the real database, with a fake node."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import SessionFactory
from app.errors import RentalFailed
from app.jellyfin_client import JellyfinUnavailable
from app.models import RentalStatus
from app.rentals import RentalSaga, SqlRentalStore

from .conftest import Seed


class FakeNodeAccess:
    def __init__(self) -> None:
        self.down = False
        self.calls = 0

    async def sync(self, user_id: int, node_id: int) -> bool:
        self.calls += 1
        if self.down:
            raise JellyfinUnavailable(f"jf{node_id}", "down")
        return True

    async def stop_playback(self, user_id: int, node_id: int, jf_item_id: uuid.UUID) -> int:
        if self.down:
            raise JellyfinUnavailable(f"jf{node_id}", "down")
        return 0


@pytest.fixture
def access() -> FakeNodeAccess:
    return FakeNodeAccess()


@pytest.fixture
def saga(sf: SessionFactory, access: FakeNodeAccess) -> RentalSaga:
    return RentalSaga(SqlRentalStore(sf), access, pending_timeout=timedelta(seconds=30))


async def status_of(engine: AsyncEngine, rental_id: int) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT status::text FROM rental WHERE id = :id"), {"id": rental_id}
        )
        return str(result.scalar_one())


async def test_rent_and_return(saga: RentalSaga, seed: Seed, engine: AsyncEngine) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    rental = await saga.rent(await seed.user("alice"), title)
    assert rental.status is RentalStatus.ACTIVE
    closed = await saga.return_rental(rental.id)
    assert closed.status is RentalStatus.RETURNED
    assert await status_of(engine, rental.id) == "RETURNED"


async def test_failed_grant_releases_the_copy(
    saga: RentalSaga, access: FakeNodeAccess, seed: Seed
) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    alice, bob = await seed.user("alice"), await seed.user("bob")
    access.down = True
    with pytest.raises(RentalFailed):
        await saga.rent(alice, title)
    access.down = False
    rental = await saga.rent(bob, title)  # the compensated copy is free again
    assert rental.status is RentalStatus.ACTIVE


async def test_return_waits_for_the_node_and_repair_finishes_it(
    saga: RentalSaga, access: FakeNodeAccess, seed: Seed, engine: AsyncEngine
) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    alice, bob = await seed.user("alice"), await seed.user("bob")
    rental = await saga.rent(alice, title)

    access.down = True
    result = await saga.return_rental(rental.id)
    assert result.status is RentalStatus.RETURNING
    # The copy stays locked while the previous renter may still have access.
    with pytest.raises(Exception, match="NO_COPY_AVAILABLE|No free copy"):
        await saga.rent(bob, title)

    access.down = False
    async with engine.begin() as conn:  # age the rental past the repair threshold
        await conn.execute(
            text("UPDATE rental SET updated_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": rental.id},
        )
    report = await saga.repair()
    assert report.returns_completed == 1
    assert await status_of(engine, rental.id) == "RETURNED"


async def test_overdue_rentals_expire(saga: RentalSaga, seed: Seed, engine: AsyncEngine) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    rental = await saga.rent(await seed.user("alice"), title, hours=1)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE rental SET rented_at = now() - interval '2 hours', "
                 "due_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": rental.id},
        )
    assert await saga.expire_overdue() == 1
    assert await status_of(engine, rental.id) == "EXPIRED"
