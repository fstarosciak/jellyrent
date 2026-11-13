"""Rental saga: renting and returning across the central DB and a Jellyfin node.

A rental changes two systems that share no transaction manager: the central
PostgreSQL database and the Jellyfin node's user policy. Jellyfin's REST API
cannot take part in two-phase commit, so the operation is a Saga made of local
transactions plus compensations, with the intermediate states persisted:

    rent:    [DB] rent_title() -> PENDING     (reserve the copy; committed)
             [node] grant: reconcile AllowedTags (now includes the copy tag)
             [DB] activate_rental() -> ACTIVE
             on node failure: [DB] fail_rental() -> FAILED, best-effort revoke

    return:  [DB] begin_return() -> RETURNING (the copy stays locked)
             [node] revoke: reconcile AllowedTags, stop playback
             [DB] finish_return() -> RETURNED | EXPIRED
             on node failure: stays RETURNING; the worker retries

Every step is idempotent and every DB transition is a compare-and-set, so the
API and the worker may safely run the same step concurrently. The orchestrator
depends on two small ports (RentalStore, AccessGateway) so it can be unit
tested without a database or Jellyfin.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError

from app.db import SessionFactory
from app.errors import InvalidTransition, RentalFailed, translate_db_error
from app.jellyfin_client import JellyfinError
from app.models import RentalStatus, ReturnReason

log = logging.getLogger(__name__)


# ============================================================================ model


@dataclass(frozen=True, slots=True)
class RentalRecord:
    id: int
    user_id: int
    copy_id: int
    node_id: int
    jf_item_id: uuid.UUID
    status: RentalStatus
    return_reason: ReturnReason | None
    due_at: datetime


@dataclass(slots=True)
class RepairReport:
    compensated: int = 0
    returns_completed: int = 0
    returns_pending: int = 0


# ============================================================================ ports


class RentalStore(Protocol):
    async def reserve(
        self, user_id: int, title_id: int, node_id: int | None, hours: int
    ) -> RentalRecord: ...
    async def activate(self, rental_id: int) -> RentalRecord | None: ...
    async def fail(self, rental_id: int) -> RentalRecord | None: ...
    async def begin_return(self, rental_id: int, reason: ReturnReason) -> RentalRecord: ...
    async def finish_return(self, rental_id: int) -> RentalRecord: ...
    async def get(self, rental_id: int) -> RentalRecord | None: ...
    async def stale_pending(self, older_than: timedelta) -> list[RentalRecord]: ...
    async def stuck_returning(self, older_than: timedelta) -> list[RentalRecord]: ...
    async def overdue(self) -> list[RentalRecord]: ...


class AccessGateway(Protocol):
    async def sync(self, user_id: int, node_id: int) -> bool: ...
    async def stop_playback(self, user_id: int, node_id: int, jf_item_id: uuid.UUID) -> int: ...


# ===================================================================== orchestrator


class RentalSaga:
    def __init__(
        self,
        store: RentalStore,
        access: AccessGateway,
        *,
        default_hours: int = 48,
        pending_timeout: timedelta = timedelta(minutes=2),
    ) -> None:
        self._store = store
        self._access = access
        self._default_hours = default_hours
        self._pending_timeout = pending_timeout

    # -------------------------------------------------------------------- rent
    async def rent(
        self, user_id: int, title_id: int, node_id: int | None = None, hours: int | None = None
    ) -> RentalRecord:
        # Step 1 - local transaction: pick and lock a copy. Domain errors
        # (limit, no copy, ...) surface here before anything remote happens.
        rental = await self._store.reserve(user_id, title_id, node_id, hours or self._default_hours)
        log.info("rental %s reserved copy %s on node %s", rental.id, rental.copy_id, rental.node_id)

        # Step 2 - remote: grant access on the node.
        try:
            await self._access.sync(rental.user_id, rental.node_id)
        except JellyfinError as exc:
            log.warning("rental %s: granting access failed (%s), compensating", rental.id, exc)
            await self._compensate(rental)
            raise RentalFailed(str(exc)) from exc

        # Step 3 - local transaction: confirm.
        active = await self._store.activate(rental.id)
        if active is None:
            # The repair job gave up on this rental meanwhile (PENDING timeout).
            # Its compensation may have run before our grant: revoke again.
            await self._revoke_best_effort(rental)
            raise RentalFailed("the rental timed out while access was being granted")
        log.info("rental %s active", rental.id)
        return active

    async def _compensate(self, rental: RentalRecord) -> None:
        """Undo a reservation: release the copy, then revoke access if possible."""
        if await self._store.fail(rental.id) is not None:
            log.info("rental %s compensated (FAILED)", rental.id)
        # Whether or not we won the CAS, make sure no stale grant survives.
        await self._revoke_best_effort(rental)

    async def _revoke_best_effort(self, rental: RentalRecord) -> None:
        try:
            await self._access.sync(rental.user_id, rental.node_id)
        except JellyfinError as exc:
            # The node is down. Its copies are unavailable while it is OFFLINE,
            # and the node is reconciled before it is marked ONLINE again, so
            # the stale grant cannot outlive the outage.
            log.warning("rental %s: revoke deferred until node recovers (%s)", rental.id, exc)

    # ------------------------------------------------------------------ return
    async def return_rental(
        self, rental_id: int, reason: ReturnReason = ReturnReason.USER
    ) -> RentalRecord:
        rental = await self._store.begin_return(rental_id, reason)
        return await self.complete_return(rental)

    async def complete_return(self, rental: RentalRecord) -> RentalRecord:
        """Revoke access on the node and close the rental.

        Leaves the rental in RETURNING (and the copy locked) if the node is
        unreachable - the copy must not be handed out while the previous
        renter may still have access.
        """
        try:
            await self._access.sync(rental.user_id, rental.node_id)
            await self._access.stop_playback(rental.user_id, rental.node_id, rental.jf_item_id)
        except JellyfinError as exc:
            log.warning("rental %s stays RETURNING, node unreachable: %s", rental.id, exc)
            return rental
        closed = await self._store.finish_return(rental.id)
        log.info("rental %s closed as %s", rental.id, closed.status)
        return closed

    # ------------------------------------------------------------- worker jobs
    async def expire_overdue(self) -> int:
        """ACTIVE rentals past due_at go through the return path as EXPIRED."""
        expired = 0
        for rental in await self._store.overdue():
            try:
                returning = await self._store.begin_return(rental.id, ReturnReason.EXPIRED)
            except InvalidTransition:
                continue  # returned concurrently
            result = await self.complete_return(returning)
            if result.status is RentalStatus.EXPIRED:
                expired += 1
        return expired

    async def repair(self) -> RepairReport:
        """Drive sagas left in an intermediate state to a final state."""
        report = RepairReport()
        for rental in await self._store.stale_pending(self._pending_timeout):
            await self._compensate(rental)
            report.compensated += 1
        for rental in await self._store.stuck_returning(timedelta(seconds=10)):
            result = await self.complete_return(rental)
            if result.status is RentalStatus.RETURNING:
                report.returns_pending += 1
            else:
                report.returns_completed += 1
        return report


# ======================================================================== adapters


_RECORD_SQL = """
    SELECT r.id, r.user_id, r.copy_id, c.node_id, c.jf_item_id,
           r.status::text AS status, r.return_reason::text AS return_reason, r.due_at
      FROM {source} r
      JOIN copy c ON c.id = r.copy_id
"""


def _record(row: Row[Any]) -> RentalRecord:
    return RentalRecord(
        id=row.id,
        user_id=row.user_id,
        copy_id=row.copy_id,
        node_id=row.node_id,
        jf_item_id=row.jf_item_id,
        status=RentalStatus(row.status),
        return_reason=ReturnReason(row.return_reason) if row.return_reason else None,
        due_at=row.due_at,
    )


class SqlRentalStore:
    """RentalStore backed by the PL/pgSQL rental functions."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def _one_from_function(self, call: str, params: dict[str, Any]) -> RentalRecord | None:
        # `call` and the fragments are constants of this module, never user input.
        sql = text(f"WITH r AS (SELECT * FROM {call}) " + _RECORD_SQL.format(source="r"))  # noqa: S608
        async with self._sf() as session, session.begin():
            try:
                row = (await session.execute(sql, params)).one_or_none()
            except DBAPIError as exc:
                domain_error = translate_db_error(exc)
                if domain_error is not None:
                    raise domain_error from exc
                raise
        return _record(row) if row is not None else None

    async def _required(self, call: str, params: dict[str, Any]) -> RentalRecord:
        record = await self._one_from_function(call, params)
        if record is None:  # functions returning a single row always return one
            raise RuntimeError(f"{call} returned no row")
        return record

    async def reserve(
        self, user_id: int, title_id: int, node_id: int | None, hours: int
    ) -> RentalRecord:
        return await self._required(
            "rent_title(CAST(:user_id AS int), CAST(:title_id AS bigint), "
            "CAST(:node_id AS int), CAST(:hours AS int))",
            {"user_id": user_id, "title_id": title_id, "node_id": node_id, "hours": hours},
        )

    async def activate(self, rental_id: int) -> RentalRecord | None:
        return await self._one_from_function(
            "activate_rental(CAST(:id AS bigint))", {"id": rental_id}
        )

    async def fail(self, rental_id: int) -> RentalRecord | None:
        return await self._one_from_function("fail_rental(CAST(:id AS bigint))", {"id": rental_id})

    async def begin_return(self, rental_id: int, reason: ReturnReason) -> RentalRecord:
        return await self._required(
            "begin_return(CAST(:id AS bigint), CAST(:reason AS return_reason))",
            {"id": rental_id, "reason": reason.value},
        )

    async def finish_return(self, rental_id: int) -> RentalRecord:
        return await self._required("finish_return(CAST(:id AS bigint))", {"id": rental_id})

    async def _select(self, where: str, params: dict[str, Any]) -> list[RentalRecord]:
        sql = text(_RECORD_SQL.format(source="rental") + f" WHERE {where} ORDER BY r.id")
        async with self._sf() as session:
            rows = (await session.execute(sql, params)).all()
        return [_record(row) for row in rows]

    async def get(self, rental_id: int) -> RentalRecord | None:
        found = await self._select("r.id = CAST(:id AS bigint)", {"id": rental_id})
        return found[0] if found else None

    async def stale_pending(self, older_than: timedelta) -> list[RentalRecord]:
        return await self._select(
            "r.status = 'PENDING' AND r.rented_at < now() - CAST(:age AS interval)",
            {"age": older_than},
        )

    async def stuck_returning(self, older_than: timedelta) -> list[RentalRecord]:
        return await self._select(
            "r.status = 'RETURNING' AND r.updated_at < now() - CAST(:age AS interval)",
            {"age": older_than},
        )

    async def overdue(self) -> list[RentalRecord]:
        return await self._select("r.status = 'ACTIVE' AND r.due_at < now()", {})
