"""Catalog sync: replicate node libraries into the central catalog.

For every node, one local transaction:
  1. read the node's fragment (FDW or REST, see app.catalog_sources),
  2. match each file to a Title: IMDb -> TMDb -> (normalised name, year),
  3. upsert the Copy by (node_id, jf_item_id),
  4. mark copies that disappeared from the node as not present (never deleted:
     rental history references them),
  5. stamp node.last_sync_at.
Afterwards every copy gets its access tag (rent-<copy_id>) on its node.

Nodes are processed independently: one unreadable node never blocks the
others, and its copies keep their last known state (we only mark copies
missing when the node's library was actually read). The sync is
single-flight across processes (session-level advisory lock).

Replication is periodic, so the catalog is eventually consistent with the
nodes. Availability decisions use node.status, which the heartbeat keeps
fresh, so a stale catalog cannot hand out copies on a dead node.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.access import access_tag
from app.catalog_sources import CatalogEntry, CatalogSource, CatalogSourceError
from app.db import SessionFactory
from app.errors import SyncInProgress
from app.jellyfin_client import JellyfinError
from app.locks import lock_key, try_session_lock
from app.models import Copy, Node, ProviderIds, Title
from app.node_registry import NodeClients, NodeRef, load_nodes

log = logging.getLogger(__name__)

SYNC_LOCK = lock_key("catalog-sync")


# ===================================================================== matching


def normalize_title(name: str) -> str:
    """Case-, accent- and punctuation-insensitive form used for fallback matching."""
    ascii_only = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.casefold()).strip()


@dataclass(slots=True)
class TitleRecord:
    id: int
    name: str
    year: int | None
    imdb: str | None
    tmdb: str | None
    genres: tuple[str, ...] = ()
    rating: Decimal | None = None


def _compatible(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


class TitleIndex:
    """In-memory index of titles for matching (the catalog is small).

    Matching order (requirement F2): IMDb id, then TMDb id, then normalised
    name + year. The fallback refuses a candidate whose external ids
    contradict the entry's, so two different films sharing a name and year
    are never merged.
    """

    def __init__(self, titles: Iterable[TitleRecord] = ()) -> None:
        self._by_imdb: dict[str, TitleRecord] = {}
        self._by_tmdb: dict[str, TitleRecord] = {}
        self._by_name: dict[tuple[str, int | None], list[TitleRecord]] = {}
        for title in titles:
            self.add(title)

    def add(self, title: TitleRecord) -> None:
        if title.imdb:
            self._by_imdb[title.imdb] = title
        if title.tmdb:
            self._by_tmdb[title.tmdb] = title
        bucket = self._by_name.setdefault((normalize_title(title.name), title.year), [])
        if title not in bucket:
            bucket.append(title)

    def match(self, entry: CatalogEntry) -> TitleRecord | None:
        if entry.imdb and entry.imdb in self._by_imdb:
            return self._by_imdb[entry.imdb]
        if entry.tmdb and entry.tmdb in self._by_tmdb:
            candidate = self._by_tmdb[entry.tmdb]
            if _compatible(candidate.imdb, entry.imdb):
                return candidate
        for candidate in self._by_name.get((normalize_title(entry.name), entry.year), []):
            if _compatible(candidate.imdb, entry.imdb) and _compatible(candidate.tmdb, entry.tmdb):
                return candidate
        return None


def merge_title(title: TitleRecord, entry: CatalogEntry) -> dict[str, Any]:
    """Changes that enrich `title` with information from `entry`.

    Only fills gaps (missing ids, year, rating) and unions genres - it never
    overwrites data, so the result does not depend on node order.
    """
    changes: dict[str, Any] = {}
    imdb = title.imdb or entry.imdb
    tmdb = title.tmdb or entry.tmdb
    if (imdb, tmdb) != (title.imdb, title.tmdb):
        changes["providers"] = ProviderIds(imdb=imdb, tmdb=tmdb)
    if title.year is None and entry.year is not None:
        changes["year"] = entry.year
    if title.rating is None and entry.rating is not None:
        changes["rating"] = entry.rating
    genres = tuple(dict.fromkeys([*title.genres, *entry.genres]))
    if genres != title.genres:
        changes["genres"] = list(genres)
    return changes


# ======================================================================== report


@dataclass(slots=True)
class NodeSyncResult:
    node: str
    source: str
    entries: int = 0
    titles_created: int = 0
    copies_upserted: int = 0
    copies_missing: int = 0
    tags_applied: int = 0
    error: str | None = None


@dataclass(slots=True)
class SyncReport:
    nodes: list[NodeSyncResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(n.error is None for n in self.nodes)


# ======================================================================= service


class CatalogSync:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: SessionFactory,
        source: CatalogSource,
        clients: NodeClients,
    ) -> None:
        self._engine = engine
        self._sf = session_factory
        self._source = source
        self._clients = clients

    async def run(self) -> SyncReport:
        """Sync all nodes. Raises SyncInProgress if another sync holds the lock."""
        async with self._engine.connect() as lock_conn:
            lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
            if not await try_session_lock(lock_conn, SYNC_LOCK):
                raise SyncInProgress()
            try:
                return await self._sync_all()
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SYNC_LOCK})

    async def _sync_all(self) -> SyncReport:
        async with self._sf() as session:
            nodes = await load_nodes(session)
        report = SyncReport()
        for node in nodes:
            report.nodes.append(await self._sync_node(node))
        return report

    async def _sync_node(self, node: NodeRef) -> NodeSyncResult:
        result = NodeSyncResult(node=node.name, source=self._source.name)
        try:
            entries = await self._source.fetch(node)
        except CatalogSourceError as exc:
            result.error = str(exc)
            log.warning("sync of %s skipped: %s", node.name, exc)
            return result
        result.entries = len(entries)

        untagged = await self._apply(node, entries, result)

        if node.is_online and untagged:
            result.tags_applied = await self._tag_copies(node, untagged)
        log.info(
            "synced %s via %s: %d files, %d new titles, %d missing, %d tagged",
            node.name,
            result.source,
            result.entries,
            result.titles_created,
            result.copies_missing,
            result.tags_applied,
        )
        return result

    async def _apply(
        self, node: NodeRef, entries: list[CatalogEntry], result: NodeSyncResult
    ) -> list[tuple[int, CatalogEntry]]:
        """Write one node's fragment in a single transaction.

        Returns (copy_id, entry) pairs whose file does not carry its access tag yet.
        """
        untagged: list[tuple[int, CatalogEntry]] = []
        async with self._sf() as session, session.begin():
            index = TitleIndex(await self._load_titles(session))
            seen: list[Any] = []
            for entry in entries:
                title_id = await self._resolve_title(session, index, entry, result)
                copy_id = await self._upsert_copy(session, node.id, title_id, entry)
                result.copies_upserted += 1
                seen.append(entry.jf_item_id)
                if access_tag(copy_id) not in entry.tags:
                    untagged.append((copy_id, entry))

            missing = await session.execute(
                update(Copy)
                .where(Copy.node_id == node.id, Copy.is_present, Copy.jf_item_id.not_in(seen))
                .values(is_present=False)
                .returning(Copy.id)
            )
            result.copies_missing = len(missing.all())
            await session.execute(
                update(Node).where(Node.id == node.id).values(last_sync_at=func.now())
            )
        return untagged

    @staticmethod
    async def _load_titles(session: AsyncSession) -> list[TitleRecord]:
        titles = (await session.scalars(select(Title))).all()
        return [
            TitleRecord(
                id=t.id,
                name=t.name,
                year=t.year,
                imdb=t.providers.imdb if t.providers else None,
                tmdb=t.providers.tmdb if t.providers else None,
                genres=tuple(t.genres or ()),
                rating=t.rating,
            )
            for t in titles
        ]

    @staticmethod
    async def _resolve_title(
        session: AsyncSession, index: TitleIndex, entry: CatalogEntry, result: NodeSyncResult
    ) -> int:
        match = index.match(entry)
        if match is None:
            title_id = await session.scalar(
                pg_insert(Title)
                .values(
                    name=entry.name,
                    year=entry.year,
                    providers=ProviderIds(imdb=entry.imdb, tmdb=entry.tmdb),
                    genres=list(entry.genres),
                    rating=entry.rating,
                )
                .returning(Title.id)
            )
            assert title_id is not None
            index.add(
                TitleRecord(
                    id=title_id,
                    name=entry.name,
                    year=entry.year,
                    imdb=entry.imdb,
                    tmdb=entry.tmdb,
                    genres=entry.genres,
                    rating=entry.rating,
                )
            )
            result.titles_created += 1
            return int(title_id)

        changes = merge_title(match, entry)
        if changes:
            await session.execute(update(Title).where(Title.id == match.id).values(**changes))
            enriched = replace(
                match,
                year=changes.get("year", match.year),
                rating=changes.get("rating", match.rating),
                genres=tuple(changes.get("genres", match.genres)),
                imdb=changes["providers"].imdb if "providers" in changes else match.imdb,
                tmdb=changes["providers"].tmdb if "providers" in changes else match.tmdb,
            )
            index.add(enriched)
        return match.id

    @staticmethod
    async def _upsert_copy(
        session: AsyncSession, node_id: int, title_id: int, entry: CatalogEntry
    ) -> int:
        insert_stmt = pg_insert(Copy).values(
            title_id=title_id,
            node_id=node_id,
            jf_item_id=entry.jf_item_id,
            path=entry.path,
            media=entry.media,
            is_present=True,
        )
        upsert = insert_stmt.on_conflict_do_update(
            constraint="copy_node_item_uq",
            set_={
                "title_id": insert_stmt.excluded.title_id,
                "path": insert_stmt.excluded.path,
                "media": insert_stmt.excluded.media,
                "is_present": True,
                "last_seen_at": func.now(),
            },
        ).returning(Copy.id)
        copy_id = await session.scalar(upsert)
        assert copy_id is not None
        return int(copy_id)

    async def _tag_copies(self, node: NodeRef, untagged: list[tuple[int, CatalogEntry]]) -> int:
        tagged = 0
        try:
            client = self._clients.client_for(node)
            for copy_id, entry in untagged:
                if await client.ensure_item_tag(entry.jf_item_id, access_tag(copy_id)):
                    tagged += 1
        except JellyfinError as exc:
            log.warning("tagging on %s interrupted (will retry next sync): %s", node.name, exc)
        return tagged
