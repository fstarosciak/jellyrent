"""Node directory: topology from the central database + one HTTP client per node."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFound
from app.jellyfin_client import JellyfinClient, JellyfinUnavailable
from app.models import Node, NodeStatus


@dataclass(frozen=True, slots=True)
class NodeRef:
    """Immutable snapshot of a node row, safe to pass between tasks."""

    id: int
    name: str
    base_url: str
    public_url: str
    api_key: str | None
    status: NodeStatus

    @property
    def is_online(self) -> bool:
        return self.status is NodeStatus.ONLINE

    @classmethod
    def from_model(cls, node: Node) -> NodeRef:
        return cls(
            id=node.id,
            name=node.name,
            base_url=node.base_url,
            public_url=node.public_url,
            api_key=node.api_key,
            status=NodeStatus(node.status),
        )


async def load_nodes(session: AsyncSession) -> list[NodeRef]:
    nodes = (await session.scalars(select(Node).order_by(Node.id))).all()
    return [NodeRef.from_model(n) for n in nodes]


async def load_node(session: AsyncSession, node_id: int) -> NodeRef:
    node = await session.get(Node, node_id)
    if node is None:
        raise NotFound(f"node {node_id}")
    return NodeRef.from_model(node)


class NodeClients:
    """Caches one JellyfinClient (connection pool) per node.

    A client is rebuilt when the node's URL or API key changes, so the API key
    written by the bootstrap job is picked up without restarts.
    """

    def __init__(
        self, *, timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._clients: dict[int, tuple[tuple[str, str], JellyfinClient]] = {}
        self._retired: list[JellyfinClient] = []

    def client_for(self, node: NodeRef) -> JellyfinClient:
        if not node.api_key:
            raise JellyfinUnavailable(node.name, "node has no API key yet (bootstrap pending)")
        fingerprint = (node.base_url, node.api_key)
        cached = self._clients.get(node.id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        if cached:
            # Callers may still hold the old client; close it at shutdown.
            self._retired.append(cached[1])
        client = JellyfinClient(
            node.base_url,
            node.api_key,
            node=node.name,
            timeout=self._timeout,
            transport=self._transport,
        )
        self._clients[node.id] = (fingerprint, client)
        return client

    async def aclose(self) -> None:
        clients = [c for _, c in self._clients.values()] + self._retired
        await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)
        self._clients.clear()
        self._retired.clear()
