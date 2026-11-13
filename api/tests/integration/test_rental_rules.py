"""Rental rules enforced by the database (via the SqlRentalStore adapter)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import SessionFactory
from app.errors import (
    AlreadyRented,
    InvalidTransition,
    NoCopyAvailable,
    NotFound,
    RentalLimitReached,
)
from app.models import RentalStatus, ReturnReason
from app.rentals import SqlRentalStore

from .conftest import Seed

S = RentalStatus


@pytest.fixture
def store(sf: SessionFactory) -> SqlRentalStore:
    return SqlRentalStore(sf)


async def test_reserve_creates_pending_rental(store: SqlRentalStore, seed: Seed) -> None:
    title = await seed.title("Sintel")
    copy = await seed.copy(title, node_id=2)
    user = await seed.user("alice")
    rental = await store.reserve(user, title, None, 48)
    assert (rental.status, rental.copy_id, rental.node_id) == (S.PENDING, copy, 2)


async def test_second_renter_gets_the_other_copy_then_none_left(
    store: SqlRentalStore, seed: Seed
) -> None:
    """Spec scenarios 1-3: Ala gets a copy, Bartek the other one, Celina none."""
    title = await seed.title("Sintel")
    await seed.copy(title, node_id=1)
    await seed.copy(title, node_id=2)
    ala, bartek, celina = await seed.user("ala"), await seed.user("bartek"), await seed.user("celina")
    first = await store.reserve(ala, title, None, 48)
    second = await store.reserve(bartek, title, None, 48)
    assert {first.node_id, second.node_id} == {1, 2}
    with pytest.raises(NoCopyAvailable):
        await store.reserve(celina, title, None, 48)


async def test_least_loaded_node_is_preferred(store: SqlRentalStore, seed: Seed) -> None:
    busy_title = await seed.title("Elephants Dream", year=2006)
    await seed.copy(busy_title, node_id=1)
    title = await seed.title("Big Buck Bunny", year=2008)
    await seed.copy(title, node_id=1)
    await seed.copy(title, node_id=2)
    await store.reserve(await seed.user("bob"), busy_title, None, 48)  # node 1 now has a rental
    rental = await store.reserve(await seed.user("alice"), title, None, 48)
    assert rental.node_id == 2


async def test_rental_limit(store: SqlRentalStore, seed: Seed) -> None:
    user = await seed.user("alice", max_rentals=1)
    first, second = await seed.title("A"), await seed.title("B")
    await seed.copy(first, 1)
    await seed.copy(second, 1)
    await store.reserve(user, first, None, 48)
    with pytest.raises(RentalLimitReached):
        await store.reserve(user, second, None, 48)


async def test_same_title_cannot_be_rented_twice(store: SqlRentalStore, seed: Seed) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    await seed.copy(title, 2)
    user = await seed.user("alice")
    await store.reserve(user, title, None, 48)
    with pytest.raises(AlreadyRented):
        await store.reserve(user, title, None, 48)


async def test_offline_nodes_are_skipped(store: SqlRentalStore, seed: Seed) -> None:
    title = await seed.title("Tears of Steel", year=2012)
    await seed.copy(title, 2)
    await seed.copy(title, 3)
    await seed.set_node_status(3, "OFFLINE")
    user = await seed.user("alice")
    with pytest.raises(NoCopyAvailable):
        await store.reserve(user, title, 3, 48)  # pinned to the offline node
    rental = await store.reserve(user, title, None, 48)
    assert rental.node_id == 2


async def test_copies_on_nodes_without_an_account_are_skipped(
    store: SqlRentalStore, seed: Seed
) -> None:
    title = await seed.title("Spring", year=2019)
    await seed.copy(title, 3)
    user = await seed.user("alice", nodes=(1, 2))
    with pytest.raises(NoCopyAvailable):
        await store.reserve(user, title, None, 48)


async def test_unknown_user(store: SqlRentalStore, seed: Seed) -> None:
    title = await seed.title("Sintel")
    with pytest.raises(NotFound):
        await store.reserve(999, title, None, 48)


async def test_full_lifecycle_and_saga_log(
    store: SqlRentalStore, seed: Seed, engine: AsyncEngine
) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    rental = await store.reserve(await seed.user("alice"), title, None, 48)

    active = await store.activate(rental.id)
    assert active is not None and active.status is S.ACTIVE
    assert await store.activate(rental.id) is None  # compare-and-set
    assert await store.fail(rental.id) is None

    returning = await store.begin_return(rental.id, ReturnReason.USER)
    assert returning.status is S.RETURNING
    assert (await store.begin_return(rental.id, ReturnReason.USER)).status is S.RETURNING
    closed = await store.finish_return(rental.id)
    assert closed.status is S.RETURNED
    assert (await store.finish_return(rental.id)).status is S.RETURNED  # idempotent

    async with engine.connect() as conn:
        events = (
            await conn.execute(
                text("SELECT from_status::text, to_status::text FROM rental_event "
                     "WHERE rental_id = :id ORDER BY id"),
                {"id": rental.id},
            )
        ).all()
        returned_at = (
            await conn.execute(text("SELECT returned_at FROM rental WHERE id = :id"), {"id": rental.id})
        ).scalar()
    assert [tuple(e) for e in events] == [
        (None, "PENDING"), ("PENDING", "ACTIVE"), ("ACTIVE", "RETURNING"), ("RETURNING", "RETURNED")
    ]
    assert returned_at is not None


async def test_expiry_ends_as_expired(store: SqlRentalStore, seed: Seed) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    rental = await store.reserve(await seed.user("alice"), title, None, 48)
    await store.activate(rental.id)
    await store.begin_return(rental.id, ReturnReason.EXPIRED)
    assert (await store.finish_return(rental.id)).status is S.EXPIRED


async def test_state_machine_rejects_illegal_transitions(
    store: SqlRentalStore, seed: Seed, engine: AsyncEngine
) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    rental = await store.reserve(await seed.user("alice"), title, None, 48)
    with pytest.raises(InvalidTransition):
        await store.begin_return(rental.id, ReturnReason.USER)  # PENDING cannot be returned
    with pytest.raises(DBAPIError, match="INVALID_TRANSITION"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE rental SET status = 'RETURNED', return_reason = 'USER' WHERE id = :id"),
                {"id": rental.id},
            )


async def test_partial_unique_index_is_the_last_line_of_defence(
    seed: Seed, engine: AsyncEngine
) -> None:
    title = await seed.title("Sintel")
    copy = await seed.copy(title, 1)
    alice, bob = await seed.user("alice"), await seed.user("bob")
    insert = text(
        "INSERT INTO rental (copy_id, user_id, due_at) VALUES (:c, :u, now() + interval '1 day')"
    )
    async with engine.begin() as conn:
        await conn.execute(insert, {"c": copy, "u": alice})
    with pytest.raises(IntegrityError, match="one_active_rental_per_copy"):
        async with engine.begin() as conn:
            await conn.execute(insert, {"c": copy, "u": bob})
