"""Sources the catalog sync reads a node's library from.

* FdwCatalogSource  - SQL over the node's SQLite file via sqlite_fdw (the
                      distributed-database path: node_<name>.movies)
* RestCatalogSource - Jellyfin REST API (works without file access)
* FallbackCatalogSource - FDW first, REST if the fragment cannot be read
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import SessionFactory
from app.jellyfin_client import JellyfinError
from app.models import MediaInfo
from app.node_registry import NodeClients, NodeRef

log = logging.getLogger(__name__)

_NODE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")  # same rule as the node.name CHECK


class CatalogSourceError(Exception):
    """The node's library could not be read from this source."""


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One movie file on one node, in the broker's vocabulary."""

    jf_item_id: uuid.UUID
    name: str
    year: int | None
    path: str | None
    imdb: str | None
    tmdb: str | None
    genres: tuple[str, ...]
    tags: tuple[str, ...]
    rating: Decimal | None
    media: MediaInfo


class CatalogSource(Protocol):
    name: str

    async def fetch(self, node: NodeRef) -> list[CatalogEntry]: ...


def _container_from_path(path: str | None) -> str | None:
    suffix = PurePosixPath(path).suffix if path else ""
    return suffix[1:].lower() or None


class FdwCatalogSource:
    name = "fdw"

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def fetch(self, node: NodeRef) -> list[CatalogEntry]:
        if not _NODE_NAME.match(node.name):
            raise CatalogSourceError(f"unsafe node name {node.name!r}")
        # The schema name cannot be a bind parameter; it is validated above.
        sql = text(
            f'SELECT * FROM "node_{node.name}".movies'  # noqa: S608
        )
        try:
            async with self._sf() as session:
                rows = (await session.execute(sql)).mappings().all()
        except DBAPIError as exc:
            raise CatalogSourceError(
                f"cannot read SQLite fragment of {node.name}: {exc.orig}"
            ) from exc
        return [
            CatalogEntry(
                jf_item_id=row["jf_item_id"],
                name=row["name"],
                year=row["year"],
                path=row["path"],
                imdb=row["imdb"],
                tmdb=row["tmdb"],
                genres=tuple(row["genres"] or ()),
                tags=tuple(row["tags"] or ()),
                rating=row["rating"],
                media=MediaInfo(
                    container=row["container"],
                    video_codec=row["video_codec"],
                    width=row["width"],
                    height=row["height"],
                    runtime_sec=row["runtime_sec"],
                ),
            )
            for row in rows
            if row["name"]
        ]


def entry_from_rest(item: dict[str, Any]) -> CatalogEntry:
    providers = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items() if v}
    video: dict[str, Any] = next(
        (s for s in item.get("MediaStreams") or [] if s.get("Type") == "Video"),
        {},
    )
    ticks = item.get("RunTimeTicks")
    rating = item.get("CommunityRating")
    return CatalogEntry(
        jf_item_id=uuid.UUID(item["Id"]),
        name=item["Name"],
        year=item.get("ProductionYear"),
        path=item.get("Path"),
        imdb=providers.get("imdb"),
        tmdb=providers.get("tmdb"),
        genres=tuple(item.get("Genres") or ()),
        tags=tuple(item.get("Tags") or ()),
        rating=round(Decimal(str(rating)), 1) if rating is not None else None,
        media=MediaInfo(
            container=_container_from_path(item.get("Path")),
            video_codec=video.get("Codec"),
            width=item.get("Width") or video.get("Width"),
            height=item.get("Height") or video.get("Height"),
            runtime_sec=int(ticks // 10_000_000) if ticks else None,
        ),
    )


class RestCatalogSource:
    name = "rest"

    def __init__(self, clients: NodeClients) -> None:
        self._clients = clients

    async def fetch(self, node: NodeRef) -> list[CatalogEntry]:
        try:
            items = await self._clients.client_for(node).list_movies()
        except JellyfinError as exc:
            raise CatalogSourceError(str(exc)) from exc
        return [entry_from_rest(item) for item in items if item.get("Name")]


class FallbackCatalogSource:
    def __init__(self, primary: CatalogSource, fallback: CatalogSource) -> None:
        self._primary = primary
        self._fallback = fallback
        self.name = f"{primary.name}->{fallback.name}"

    async def fetch(self, node: NodeRef) -> list[CatalogEntry]:
        try:
            return await self._primary.fetch(node)
        except CatalogSourceError as exc:
            log.warning(
                "%s source failed for %s, using %s: %s",
                self._primary.name,
                node.name,
                self._fallback.name,
                exc,
            )
            return await self._fallback.fetch(node)
