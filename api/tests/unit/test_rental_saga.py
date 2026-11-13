"""Saga orchestration with in-memory fakes for the database and the node."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.errors import InvalidTransition, NoCopyAvailable, RentalFailed
from app.jellyfin_client import JellyfinUnavailable
from app.models import RentalStatus, ReturnReason
from app.rentals import RentalRecord, RentalSaga

S = RentalStatus


class FakeStore:
    """Mimics the PL/pgSQL functions: compare-and-set transitions."""

    def __init__(self) -> None:
        self.rentals: dict[int, RentalRecord] = {}
        self.rented_at: dict[int, datetime] = {}
        self.next_id = 1
        self.reserve_error: Exception | None = None
        self.on_activate = None  # hook to simulate a concurrent repair

    def add(self, status: RentalStatus, *, age: timedelta = timedelta(0), **kw: object) -> RentalRecord:
        record = RentalRecord(
            id=self.next_id,
            user_id=kw.get("user_id", 1),  # type: ignore[arg-type]
            copy_id=10 + self.next_id,
            node_id=kw.get("node_id", 2),  # type: ignore[arg-type]
            jf_item_id=uuid.uuid4(),
            status=status,
            return_reason=kw.get("return_reason"),  # type: ignore[arg-type]
            due_at=kw.get("due_at", datetime.now(UTC) + timedelta(hours=48)),  # type: ignore[arg-type]
        )
        self.rentals[record.id] = record
        self.rented_at[record.id] = datetime.now(UTC) - age
        self.next_id += 1
        return record

    def _cas(self, rental_id: int, expected: RentalStatus, target: RentalStatus, **kw: object) -> RentalRecord | None:
        current = self.rentals[rental_id]
        if current.status is not expected:
            return None
        updated = replace(current, status=target, **kw)  # type: ignore[arg-type]
        self.rentals[rental_id] = updated
        return updated

    async def reserve(self, user_id: int, title_id: int, node_id: int | None, hours: int) -> RentalRecord:
        if self.reserve_error:
            raise self.reserve_error
        return self.add(S.PENDING, user_id=user_id, node_id=node_id or 2)

    async def activate(self, rental_id: int) -> RentalRecord | None:
        if self.on_activate:
            self.on_activate(rental_id)
        return self._cas(rental_id, S.PENDING, S.ACTIVE)

    async def fail(self, rental_id: int) -> RentalRecord | None:
        return self._cas(rental_id, S.PENDING, S.FAILED)

    async def begin_return(self, rental_id: int, reason: ReturnReason) -> RentalRecord:
        current = self.rentals[rental_id]
        if current.status is S.RETURNING:
            return current
        updated = self._cas(rental_id, S.ACTIVE, S.RETURNING, return_reason=reason)
        if updated is None:
            raise InvalidTransition(f"cannot return a {current.status} rental")
        return updated

    async def finish_return(self, rental_id: int) -> RentalRecord:
        current = self.rentals[rental_id]
        if current.status in (S.RETURNED, S.EXPIRED):
            return current
        target = S.EXPIRED if current.return_reason is ReturnReason.EXPIRED else S.RETURNED
        updated = self._cas(rental_id, S.RETURNING, target)
        assert updated is not None
        return updated

    async def get(self, rental_id: int) -> RentalRecord | None:
        return self.rentals.get(rental_id)

    async def stale_pending(self, older_than: timedelta) -> list[RentalRecord]:
        cutoff = datetime.now(UTC) - older_than
        return [r for r in self.rentals.values() if r.status is S.PENDING and self.rented_at[r.id] < cutoff]

    async def stuck_returning(self, older_than: timedelta) -> list[RentalRecord]:
        return [r for r in self.rentals.values() if r.status is S.RETURNING]

    async def overdue(self) -> list[RentalRecord]:
        now = datetime.now(UTC)
        return [r for r in self.rentals.values() if r.status is S.ACTIVE and r.due_at < now]


class FakeAccess:
    """Records calls; `down` makes the node unreachable."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.down = False
        self.failures_left = 0  # fail the next N sync calls
        self.syncs: list[tuple[int, int, frozenset[int]]] = []
        self.stopped: list[uuid.UUID] = []

    def granted(self, user_id: int, node_id: int) -> frozenset[int]:
        return frozenset(
            r.copy_id for r in self.store.rentals.values()
            if r.user_id == user_id and r.node_id == node_id and r.status in (S.PENDING, S.ACTIVE)
        )

    async def sync(self, user_id: int, node_id: int) -> bool:
        if self.down or self.failures_left > 0:
            self.failures_left = max(0, self.failures_left - 1)
            raise JellyfinUnavailable(f"jf{node_id}", "connection refused")
        self.syncs.append((user_id, node_id, self.granted(user_id, node_id)))
        return True

    async def stop_playback(self, user_id: int, node_id: int, jf_item_id: uuid.UUID) -> int:
        if self.down:
            raise JellyfinUnavailable(f"jf{node_id}", "connection refused")
        self.stopped.append(jf_item_id)
        return 1


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def access(store: FakeStore) -> FakeAccess:
    return FakeAccess(store)


@pytest.fixture
def saga(store: FakeStore, access: FakeAccess) -> RentalSaga:
    return RentalSaga(store, access, default_hours=48, pending_timeout=timedelta(minutes=2))


# ---------------------------------------------------------------------- rent


async def test_rent_grants_access_then_activates(saga: RentalSaga, access: FakeAccess) -> None:
    rental = await saga.rent(user_id=1, title_id=5)
    assert rental.status is S.ACTIVE
    # Access was granted while the rental was PENDING (step 2 before step 3).
    assert access.syncs == [(1, 2, frozenset({rental.copy_id}))]


async def test_rent_domain_errors_happen_before_any_remote_call(
    saga: RentalSaga, store: FakeStore, access: FakeAccess
) -> None:
    store.reserve_error = NoCopyAvailable()
    with pytest.raises(NoCopyAvailable):
        await saga.rent(user_id=1, title_id=5)
    assert access.syncs == []


async def test_rent_compensates_when_node_is_down(
    saga: RentalSaga, store: FakeStore, access: FakeAccess
) -> None:
    access.down = True
    with pytest.raises(RentalFailed):
        await saga.rent(user_id=1, title_id=5)
    (rental,) = store.rentals.values()
    assert rental.status is S.FAILED  # the copy is released


async def test_compensation_revokes_when_node_answers_again(
    saga: RentalSaga, store: FakeStore, access: FakeAccess
) -> None:
    """The grant call fails (e.g. timeout); the compensating revoke succeeds."""
    access.failures_left = 1
    with pytest.raises(RentalFailed):
        await saga.rent(user_id=1, title_id=5)
    assert access.syncs == [(1, 2, frozenset())]  # revoke left nothing granted


async def test_rent_fails_if_repair_job_compensated_meanwhile(
    saga: RentalSaga, store: FakeStore, access: FakeAccess
) -> None:
    store.on_activate = lambda rental_id: store._cas(rental_id, S.PENDING, S.FAILED)
    with pytest.raises(RentalFailed):
        await saga.rent(user_id=1, title_id=5)
    (rental,) = store.rentals.values()
    assert rental.status is S.FAILED
    # The grant was revoked again after losing the race.
    assert access.syncs[-1] == (1, 2, frozenset())


# -------------------------------------------------------------------- return


async def test_return_revokes_stops_playback_and_closes(
    saga: RentalSaga, store: FakeStore, access: FakeAccess
) -> None:
    rental = store.add(S.ACTIVE)
    result = await saga.return_rental(rental.id)
    assert result.status is S.RETURNED
    assert result.return_reason is ReturnReason.USER
    assert access.syncs == [(1, 2, frozenset())]
    assert access.stopped == [rental.jf_item_id]


async def test_return_stays_returning_while_node_is_down(
    saga: RentalSaga, store: FakeStore, access: FakeAccess
) -> None:
    rental = store.add(S.ACTIVE)
    access.down = True
    result = await saga.return_rental(rental.id)
    assert result.status is S.RETURNING  # copy stays locked until revoked

    access.down = False
    report = await saga.repair()
    assert report.returns_completed == 1
    assert store.rentals[rental.id].status is S.RETURNED


async def test_return_is_idempotent(saga: RentalSaga, store: FakeStore) -> None:
    rental = store.add(S.ACTIVE)
    first = await saga.return_rental(rental.id)
    with pytest.raises(InvalidTransition):
        await saga.return_rental(rental.id)  # RETURNED cannot be returned again
    assert first.status is S.RETURNED


async def test_cannot_return_pending_rental(saga: RentalSaga, store: FakeStore) -> None:
    rental = store.add(S.PENDING)
    with pytest.raises(InvalidTransition):
        await saga.return_rental(rental.id)


# --------------------------------------------------------------- worker jobs


async def test_expire_overdue(saga: RentalSaga, store: FakeStore, access: FakeAccess) -> None:
    overdue = store.add(S.ACTIVE, due_at=datetime.now(UTC) - timedelta(minutes=1))
    fresh = store.add(S.ACTIVE)
    assert await saga.expire_overdue() == 1
    assert store.rentals[overdue.id].status is S.EXPIRED
    assert store.rentals[overdue.id].return_reason is ReturnReason.EXPIRED
    assert store.rentals[fresh.id].status is S.ACTIVE


async def test_repair_compensates_stale_pending_only(saga: RentalSaga, store: FakeStore) -> None:
    stale = store.add(S.PENDING, age=timedelta(minutes=5))
    young = store.add(S.PENDING, age=timedelta(seconds=5))
    report = await saga.repair()
    assert report.compensated == 1
    assert store.rentals[stale.id].status is S.FAILED
    assert store.rentals[young.id].status is S.PENDING
