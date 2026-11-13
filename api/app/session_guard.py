"""Session guard: enforce "one disc = one viewer, one connection" (F12).

Every few seconds, per node:
  1. list playback sessions (GET /Sessions) and map the playing item to a copy,
  2. record them in playback_session (first_seen_at orders competing sessions),
  3. decide (pure function) which sessions violate the rules:
       NO_RENTAL           - the viewer holds no live rental of that copy
       CONCURRENT_SESSION  - the renter plays the copy from more than one
                             session; the oldest session wins
  4. stop the offending sessions and log violations (deduplicated).

AllowedTags already hides copies a user has not rented; the guard is the
second line of defence (e.g. a stale grant while a node was offline, an admin
or unmanaged account, a second device of the renter).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionFactory
from app.jellyfin_client import JellyfinRequestError, SessionInfo
from app.models import NodeAccount, NodeStatus, PlaybackSession, Violation, ViolationReason
from app.node_registry import NodeClients, NodeRef, load_nodes

log = logging.getLogger(__name__)

_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class TrackedCopy:
    copy_id: int
    renter_user_id: int | None  # holder of the live rental, if any
    renter_jf_user_id: uuid.UUID | None  # the holder's account on this node


@dataclass(frozen=True, slots=True)
class Enforcement:
    session_id: str
    copy_id: int
    jf_user_id: uuid.UUID | None
    user_id: int | None
    reason: ViolationReason
    detail: str


def decide(
    sessions: Iterable[SessionInfo],
    copies_by_item: Mapping[uuid.UUID, TrackedCopy],
    first_seen: Mapping[tuple[str, int], datetime],
    accounts: Mapping[uuid.UUID, int] | None = None,
) -> list[Enforcement]:
    """Pure decision logic: which sessions must be stopped and why.

    `accounts` maps Jellyfin user ids on this node to JellyRent user ids, so
    violations can be attributed to a JellyRent user when there is one.
    """
    accounts = accounts or {}
    enforcements: list[Enforcement] = []
    renter_sessions: dict[int, list[SessionInfo]] = defaultdict(list)

    for session in sessions:
        if session.now_playing_item_id is None:
            continue
        copy = copies_by_item.get(session.now_playing_item_id)
        if copy is None:
            continue  # not a rentable copy (trailer, other library, ...)
        if copy.renter_jf_user_id is None or session.user_id != copy.renter_jf_user_id:
            enforcements.append(
                Enforcement(
                    session_id=session.id,
                    copy_id=copy.copy_id,
                    jf_user_id=session.user_id,
                    user_id=accounts.get(session.user_id) if session.user_id else None,
                    reason=ViolationReason.NO_RENTAL,
                    detail=f"{session.user_name or session.user_id} has no rental of this copy",
                )
            )
            continue
        renter_sessions[copy.copy_id].append(session)

    for copy_id, group in renter_sessions.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: (first_seen.get((s.id, copy_id), _FAR_FUTURE), s.id))
        keeper = group[0]
        for extra in group[1:]:
            enforcements.append(
                Enforcement(
                    session_id=extra.id,
                    copy_id=copy_id,
                    jf_user_id=extra.user_id,
                    user_id=accounts.get(extra.user_id) if extra.user_id else None,
                    reason=ViolationReason.CONCURRENT_SESSION,
                    detail=(
                        f"second connection ({extra.device_name or extra.client}); "
                        f"session {keeper.id} keeps playing"
                    ),
                )
            )
    return enforcements


class SessionGuard:
    def __init__(self, session_factory: SessionFactory, clients: NodeClients) -> None:
        self._sf = session_factory
        self._clients = clients

    async def run(self) -> int:
        """Check all ONLINE nodes concurrently; returns the number of enforcements."""
        async with self._sf() as session:
            nodes = [n for n in await load_nodes(session) if n.status is NodeStatus.ONLINE]
        results = await asyncio.gather(*(self.guard_node(n) for n in nodes), return_exceptions=True)
        total = 0
        for node, outcome in zip(nodes, results, strict=True):
            if isinstance(outcome, BaseException):
                log.warning("session guard on %s failed: %s", node.name, outcome)
            else:
                total += outcome
        return total

    async def guard_node(self, node: NodeRef) -> int:
        client = self._clients.client_for(node)
        sessions = [s for s in await client.get_sessions() if s.now_playing_item_id]
        copies, first_seen, accounts = await self._observe(node, sessions)
        enforcements = decide(sessions, copies, first_seen, accounts)
        for enforcement in enforcements:
            await self._enforce(node, enforcement)
        if enforcements:
            await self._record(node, enforcements)
        return len(enforcements)

    async def _observe(
        self, node: NodeRef, sessions: list[SessionInfo]
    ) -> tuple[dict[uuid.UUID, TrackedCopy], dict[tuple[str, int], datetime], dict[uuid.UUID, int]]:
        """Map items to copies, viewers to users, and persist the observed sessions."""
        item_ids = list({s.now_playing_item_id for s in sessions if s.now_playing_item_id})
        viewer_ids = list({s.user_id for s in sessions if s.user_id})
        async with self._sf() as db, db.begin():
            copies: dict[uuid.UUID, TrackedCopy] = {}
            accounts: dict[uuid.UUID, int] = {}
            if viewer_ids:
                rows = await db.execute(
                    select(NodeAccount.jf_user_id, NodeAccount.user_id).where(
                        NodeAccount.node_id == node.id, NodeAccount.jf_user_id.in_(viewer_ids)
                    )
                )
                accounts = {jf_user_id: user_id for jf_user_id, user_id in rows}
            if item_ids:
                rows = await db.execute(
                    text(
                        """
                        SELECT c.id, c.jf_item_id, r.user_id, na.jf_user_id
                          FROM copy c
                          LEFT JOIN rental r
                                 ON r.copy_id = c.id
                                AND r.status IN ('PENDING', 'ACTIVE', 'RETURNING')
                          LEFT JOIN node_account na
                                 ON na.user_id = r.user_id AND na.node_id = c.node_id
                         WHERE c.node_id = CAST(:node_id AS int)
                           AND c.jf_item_id = ANY(CAST(:items AS uuid[]))
                        """
                    ),
                    {"node_id": node.id, "items": item_ids},
                )
                for row in rows:
                    copies[row.jf_item_id] = TrackedCopy(row.id, row.user_id, row.jf_user_id)

            observed = [
                (s.id, copies[s.now_playing_item_id].copy_id, s.user_id)
                for s in sessions
                if s.now_playing_item_id in copies
            ]
            first_seen: dict[tuple[str, int], datetime] = {}
            if observed:
                insert_stmt = pg_insert(PlaybackSession).values(
                    [
                        {
                            "node_id": node.id,
                            "jf_session_id": sid,
                            "copy_id": cid,
                            "jf_user_id": uid,
                        }
                        for sid, cid, uid in observed
                    ]
                )
                upsert = insert_stmt.on_conflict_do_update(
                    index_elements=["node_id", "jf_session_id", "copy_id"],
                    set_={
                        "last_seen_at": func.now(),
                        "jf_user_id": insert_stmt.excluded.jf_user_id,
                    },
                ).returning(
                    PlaybackSession.jf_session_id,
                    PlaybackSession.copy_id,
                    PlaybackSession.first_seen_at,
                )
                for sid, cid, seen_at in await db.execute(upsert):
                    first_seen[(sid, cid)] = seen_at

            # Forget sessions that are no longer playing.
            gone = delete(PlaybackSession).where(PlaybackSession.node_id == node.id)
            if observed:
                gone = gone.where(
                    tuple_(PlaybackSession.jf_session_id, PlaybackSession.copy_id).not_in(
                        [(sid, cid) for sid, cid, _ in observed]
                    )
                )
            await db.execute(gone)
        return copies, first_seen, accounts

    async def _enforce(self, node: NodeRef, enforcement: Enforcement) -> None:
        client = self._clients.client_for(node)
        log.warning(
            "stopping session %s on %s: %s (%s)",
            enforcement.session_id,
            node.name,
            enforcement.reason,
            enforcement.detail,
        )
        try:
            await client.send_message(
                enforcement.session_id,
                "JellyRent",
                "Playback stopped: this copy is not yours to watch here.",
            )
            await client.stop_session(enforcement.session_id)
        except JellyfinRequestError as exc:
            log.info("session %s could not be stopped (gone?): %s", enforcement.session_id, exc)

    async def _record(self, node: NodeRef, enforcements: list[Enforcement]) -> None:
        async with self._sf() as db, db.begin():
            stmt = pg_insert(Violation).values(
                [
                    {
                        "node_id": node.id,
                        "copy_id": e.copy_id,
                        "user_id": e.user_id,
                        "jf_user_id": e.jf_user_id,
                        "jf_session_id": e.session_id,
                        "reason": e.reason,
                        "detail": e.detail,
                    }
                    for e in enforcements
                ]
            )
            await db.execute(stmt.on_conflict_do_nothing(constraint="violation_once_per_session"))
