"""Access control on the nodes: desired-state reconciliation of AllowedTags.

The central database is the single source of truth for who may see which
copy. Instead of incrementally adding/removing a tag (a read-modify-write of
the whole Jellyfin policy, which loses updates under concurrency), every
change recomputes the complete desired tag set for a (user, node) pair and
writes it if it differs ("level-triggered" reconciliation):

    desired(user, node) = {sentinel} + {access_tag(copy) for each PENDING/ACTIVE rental}

Reconciliations of the same (user, node) are serialised with an advisory
lock, so concurrent rent/return operations cannot overwrite each other.
Because the operation is idempotent it doubles as the repair mechanism: the
worker re-runs it for every account when a node comes back and periodically.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.jellyfin_client import JellyfinRequestError
from app.locks import lock_key, xact_lock
from app.models import Node, NodeAccount
from app.node_registry import NodeClients, NodeRef

log = logging.getLogger(__name__)

ACCESS_TAG_PREFIX = "rent-"  # must match access_tag() in db/init/03_functions.sql

# Policy settings JellyRent enforces on every managed account.
ENFORCED_POLICY: dict[str, Any] = {
    "IsAdministrator": False,
    "MaxActiveSessions": 1,  # F11: one active session per user per node
    "EnableContentDeletion": False,
    "EnableContentDeletionFromFolders": [],
    "EnableContentDownloading": False,  # a downloaded "disc" could not be returned
    "EnablePublicSharing": False,
}


def access_tag(copy_id: int) -> str:
    """Tag that grants access to a copy (Python twin of SQL access_tag(copy))."""
    return f"{ACCESS_TAG_PREFIX}{copy_id}"


def is_managed_tag(tag: str, sentinel: str) -> bool:
    return tag == sentinel or tag.startswith(ACCESS_TAG_PREFIX)


def apply_access_policy(
    policy: dict[str, Any], desired_tags: set[str], sentinel: str
) -> dict[str, Any]:
    """Return a copy of `policy` with AllowedTags set to the desired state.

    Tags not managed by JellyRent are preserved. The sentinel tag is always
    present: Jellyfin treats an empty AllowedTags list as "no restriction".
    """
    current: list[str] = list(policy.get("AllowedTags") or [])
    unmanaged = [t for t in current if not is_managed_tag(t, sentinel)]
    managed = sorted(desired_tags - {sentinel})
    return {**policy, **ENFORCED_POLICY, "AllowedTags": [*unmanaged, sentinel, *managed]}


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    node: str
    accounts: int
    changed: int


class AccessManager:
    def __init__(
        self, session_factory: SessionFactory, clients: NodeClients, *, sentinel_tag: str
    ) -> None:
        self._sf = session_factory
        self._clients = clients
        self._sentinel = sentinel_tag

    async def sync(self, user_id: int, node_id: int) -> bool:
        """Make the user's AllowedTags on the node match the central state.

        Returns True if the policy was changed. Raises JellyfinError if the
        node cannot be updated.
        """
        async with self._sf() as session, session.begin():
            await xact_lock(session, lock_key("access", user_id, node_id))
            row = (
                await session.execute(
                    select(NodeAccount.jf_user_id, Node)
                    .join(Node, Node.id == NodeAccount.node_id)
                    .where(NodeAccount.user_id == user_id, NodeAccount.node_id == node_id)
                )
            ).one_or_none()
            if row is None:
                return False  # no account on this node: nothing can be visible
            jf_user_id, node = row
            desired = await self._desired_tags(session, user_id, node_id)
            # The HTTP calls run while holding the advisory lock (bounded by
            # the client timeout) - this is what serialises policy writes.
            return await self._write_policy(NodeRef.from_model(node), jf_user_id, desired)

    async def _desired_tags(self, session: AsyncSession, user_id: int, node_id: int) -> set[str]:
        result = await session.execute(
            text(
                """
                SELECT access_tag(c)
                  FROM rental r
                  JOIN copy c ON c.id = r.copy_id
                 WHERE r.user_id = CAST(:user_id AS int)
                   AND c.node_id = CAST(:node_id AS int)
                   AND r.status IN ('PENDING', 'ACTIVE')
                """
            ),
            {"user_id": user_id, "node_id": node_id},
        )
        return {self._sentinel, *result.scalars().all()}

    async def _write_policy(self, node: NodeRef, jf_user_id: uuid.UUID, desired: set[str]) -> bool:
        client = self._clients.client_for(node)
        user = await client.get_user(jf_user_id)
        policy: dict[str, Any] = user.get("Policy") or {}
        if policy.get("IsAdministrator"):
            # Never touch administrators (they are not JellyRent accounts).
            log.warning("refusing to manage admin account %s on %s", jf_user_id, node.name)
            return False
        new_policy = apply_access_policy(policy, desired, self._sentinel)
        if new_policy == policy:
            return False
        await client.update_policy(jf_user_id, new_policy)
        log.info(
            "access updated",
            extra={
                "node": node.name,
                "jf_user": str(jf_user_id),
                "tags": new_policy["AllowedTags"],
            },
        )
        return True

    async def reconcile_node(self, node_id: int) -> ReconcileReport:
        """Re-apply the desired state for every account on a node.

        Stops at the first node error (the node is evidently unreachable).
        """
        async with self._sf() as session:
            node = await session.get(Node, node_id)
            user_ids = (
                await session.scalars(
                    select(NodeAccount.user_id).where(NodeAccount.node_id == node_id)
                )
            ).all()
        changed = 0
        for user_id in user_ids:
            if await self.sync(user_id, node_id):
                changed += 1
        name = node.name if node else str(node_id)
        if changed:
            log.info("reconciled node %s: %d/%d accounts changed", name, changed, len(user_ids))
        return ReconcileReport(node=name, accounts=len(user_ids), changed=changed)

    async def stop_playback(self, user_id: int, node_id: int, jf_item_id: uuid.UUID) -> int:
        """Stop the user's sessions that are playing the given item."""
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(NodeAccount.jf_user_id, Node)
                    .join(Node, Node.id == NodeAccount.node_id)
                    .where(NodeAccount.user_id == user_id, NodeAccount.node_id == node_id)
                )
            ).one_or_none()
        if row is None:
            return 0
        jf_user_id, node = row
        client = self._clients.client_for(NodeRef.from_model(node))
        stopped = 0
        for s in await client.get_sessions():
            if s.user_id == jf_user_id and s.now_playing_item_id == jf_item_id:
                try:
                    await client.send_message(s.id, "JellyRent", "This rental has ended.")
                    await client.stop_session(s.id)
                    stopped += 1
                except JellyfinRequestError as exc:
                    # The session may have ended in the meantime.
                    log.info("could not stop session %s: %s", s.id, exc)
        return stopped
