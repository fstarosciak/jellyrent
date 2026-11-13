"""Node failure detection (heartbeat) and recovery.

* A node is declared OFFLINE after N consecutive failed pings (default 3), so
  a single slow response does not flap the node.
* OFFLINE nodes contribute no available copies (is_available() checks the node
  status), so new rentals are routed to the remaining nodes (F14, NF4).
* Recovery protocol: when an OFFLINE node answers again, every account on it is
  reconciled with the central state BEFORE the node is marked ONLINE. Grants
  that could not be revoked during the outage (failed rentals, returns) are
  therefore cleaned up before any of its copies can be rented again.
* MAINTENANCE is an administrative state the heartbeat never overrides.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import case, func, update

from app.access import AccessManager
from app.db import SessionFactory
from app.jellyfin_client import JellyfinError
from app.models import Node, NodeStatus
from app.node_registry import NodeClients, NodeRef, load_nodes

log = logging.getLogger(__name__)


class NodeMonitor:
    def __init__(
        self,
        session_factory: SessionFactory,
        clients: NodeClients,
        access: AccessManager,
        *,
        failure_threshold: int = 3,
    ) -> None:
        self._sf = session_factory
        self._clients = clients
        self._access = access
        self._threshold = failure_threshold

    async def heartbeat(self) -> None:
        async with self._sf() as session:
            nodes = await load_nodes(session)
        await asyncio.gather(*(self._check(node) for node in nodes))

    async def _check(self, node: NodeRef) -> None:
        try:
            client = self._clients.client_for(node)  # raises if no API key yet
            await client.ping()
        except JellyfinError as exc:
            await self._record_failure(node, str(exc))
            return

        if node.status is NodeStatus.OFFLINE:
            try:
                report = await self._access.reconcile_node(node.id)
            except JellyfinError as exc:
                await self._record_failure(node, f"recovery reconciliation failed: {exc}")
                return
            log.info(
                "node %s recovered (%d accounts reconciled, %d changed)",
                node.name,
                report.accounts,
                report.changed,
            )
        await self._record_success(node)

    async def _record_success(self, node: NodeRef) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                update(Node)
                .where(Node.id == node.id)
                .values(
                    consecutive_failures=0,
                    last_seen_at=func.now(),
                    last_error=None,
                    status=case(
                        (Node.status == NodeStatus.MAINTENANCE, Node.status),
                        else_=NodeStatus.ONLINE,
                    ),
                )
            )
        if node.status is NodeStatus.OFFLINE:
            log.info("node %s is ONLINE", node.name)

    async def _record_failure(self, node: NodeRef, error: str) -> None:
        async with self._sf() as session, session.begin():
            new_status = await session.scalar(
                update(Node)
                .where(Node.id == node.id)
                .values(
                    consecutive_failures=Node.consecutive_failures + 1,
                    last_error=error[:500],
                    status=case(
                        (
                            (Node.status == NodeStatus.ONLINE)
                            & (Node.consecutive_failures + 1 >= self._threshold),
                            NodeStatus.OFFLINE,
                        ),
                        else_=Node.status,
                    ),
                )
                .returning(Node.status)
            )
        if node.status is NodeStatus.ONLINE and new_status == NodeStatus.OFFLINE:
            log.warning("node %s is OFFLINE: %s", node.name, error)
        else:
            log.debug("node %s check failed: %s", node.name, error)
