"""Distributed read layer: sqlite_fdw over Jellyfin-shaped SQLite files.

The test writes two SQLite databases with the subset of the Jellyfin 12
schema we depend on into a directory that docker-compose.test.yml mounts
into the database container at /jf/test (read-only), registers them as nodes
and reads them through PostgreSQL - no Jellyfin needed.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.catalog_sources import FdwCatalogSource
from app.catalog_sync import CatalogSync
from app.db import SessionFactory
from app.node_registry import NodeClients

FIXTURE_DIR = os.environ.get("TEST_FDW_FIXTURE_DIR")
CONTAINER_DIR = "/jf/test"
MOVIE = "MediaBrowser.Controller.Entities.Movies.Movie"

pytestmark = pytest.mark.skipif(not FIXTURE_DIR, reason="TEST_FDW_FIXTURE_DIR not set")

SCHEMA = """
CREATE TABLE BaseItems (
    Id TEXT PRIMARY KEY, Type TEXT NOT NULL, Data TEXT, Path TEXT, Name TEXT,
    ProductionYear INTEGER, Genres TEXT, Tags TEXT, RunTimeTicks INTEGER,
    CommunityRating REAL, Width INTEGER, Height INTEGER, IsVirtualItem INTEGER NOT NULL,
    IsFolder INTEGER NOT NULL DEFAULT 0, Overview TEXT
);
CREATE TABLE BaseItemProviders (
    ItemId TEXT NOT NULL, ProviderId TEXT NOT NULL, ProviderValue TEXT NOT NULL,
    PRIMARY KEY (ItemId, ProviderId)
);
CREATE TABLE MediaStreamInfos (
    ItemId TEXT NOT NULL, StreamIndex INTEGER NOT NULL, StreamType INTEGER NOT NULL,
    Codec TEXT, PRIMARY KEY (ItemId, StreamIndex)
);
"""

BBB = ("Big Buck Bunny", 2008, "tt1254207", "10378")
SINTEL = ("Sintel", 2010, "tt1727587", "45745")
SPRING = ("Spring", 2019, None, None)


def write_node_db(path: Path, movies: list[tuple[str, int, str | None, str | None]]) -> dict[str, uuid.UUID]:
    path.unlink(missing_ok=True)
    ids: dict[str, uuid.UUID] = {}
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        # Non-movie rows that the fragment view must filter out.
        db.execute("INSERT INTO BaseItems (Id, Type, Name, IsVirtualItem, IsFolder) VALUES (?, ?, ?, 0, 1)",
                   (str(uuid.uuid4()).upper(), "MediaBrowser.Controller.Entities.CollectionFolder", "Movies"))
        db.execute("INSERT INTO BaseItems (Id, Type, Name, IsVirtualItem) VALUES (?, ?, ?, 1)",
                   (str(uuid.uuid4()).upper(), MOVIE, "Virtual placeholder"))
        for name, year, imdb, tmdb in movies:
            item = uuid.uuid4()
            ids[name] = item
            key = str(item).upper()  # EF Core stores GUIDs as upper-case text
            db.execute(
                "INSERT INTO BaseItems (Id, Type, Path, Name, ProductionYear, Genres, Tags, RunTimeTicks,"
                " CommunityRating, Width, Height, IsVirtualItem) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                (key, MOVIE, f"/media/{name} ({year})/{name} ({year}).mp4", name, year,
                 "Animation|Short", "", 6_000_000_000, 7.25, 1920, 1080),
            )
            for provider, value in (("Imdb", imdb), ("Tmdb", tmdb)):
                if value:
                    db.execute("INSERT INTO BaseItemProviders VALUES (?,?,?)", (key, provider, value))
            db.execute("INSERT INTO MediaStreamInfos VALUES (?, 0, 1, 'h264')", (key,))
            db.execute("INSERT INTO MediaStreamInfos VALUES (?, 1, 0, 'aac')", (key,))
    path.chmod(0o644)
    return ids


@pytest.fixture
async def fdw_nodes(engine: AsyncEngine) -> dict[str, dict[str, uuid.UUID]]:
    assert FIXTURE_DIR
    directory = Path(FIXTURE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    nodes = {
        "fdwtest1": write_node_db(directory / "fdwtest1.db", [BBB, SINTEL]),
        "fdwtest2": write_node_db(directory / "fdwtest2.db", [BBB, SPRING]),
    }
    async with engine.begin() as conn:
        for node_id, name in ((91, "fdwtest1"), (92, "fdwtest2")):
            await conn.execute(
                text("CALL register_node(:id, :name, 'http://unused', 'http://unused', :path)"),
                {"id": node_id, "name": name, "path": f"{CONTAINER_DIR}/{name}.db"},
            )
    return nodes


async def test_fragment_view_reads_and_normalises_sqlite(
    engine: AsyncEngine, fdw_nodes: dict[str, dict[str, uuid.UUID]]
) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT * FROM node_fdwtest1.movies ORDER BY name"))
        ).mappings().all()
    assert [r["name"] for r in rows] == ["Big Buck Bunny", "Sintel"]  # folder + virtual filtered
    bbb = rows[0]
    assert bbb["jf_item_id"] == fdw_nodes["fdwtest1"]["Big Buck Bunny"]
    assert (bbb["imdb"], bbb["tmdb"], bbb["year"]) == ("tt1254207", "10378", 2008)
    assert bbb["genres"] == ["Animation", "Short"]
    assert bbb["tags"] == []
    assert (bbb["container"], bbb["video_codec"], bbb["height"], bbb["runtime_sec"]) == (
        "mp4", "h264", 1080, 600
    )


async def test_location_transparent_query_across_fragments(
    engine: AsyncEngine, fdw_nodes: dict[str, dict[str, uuid.UUID]]
) -> None:
    """'On which nodes is Big Buck Bunny?' - one SQL statement."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT 'fdwtest1' AS node, name FROM node_fdwtest1.movies "
                "UNION ALL SELECT 'fdwtest2', name FROM node_fdwtest2.movies"
            )
        )
        nodes_with_bbb = sorted(node for node, name in rows if name == "Big Buck Bunny")
    assert nodes_with_bbb == ["fdwtest1", "fdwtest2"]


async def test_global_union_fails_if_any_fragment_is_unreadable(
    engine: AsyncEngine, fdw_nodes: dict[str, dict[str, uuid.UUID]]
) -> None:
    """jf1..jf3 have no SQLite file here: a distributed UNION ALL is only as
    available as its least available fragment (motivation for the catalog)."""
    with pytest.raises(DBAPIError):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT count(*) FROM global_library"))


async def test_catalog_sync_merges_copies_into_titles(
    engine: AsyncEngine, sf: SessionFactory, fdw_nodes: dict[str, dict[str, uuid.UUID]]
) -> None:
    clients = NodeClients()
    sync = CatalogSync(engine, sf, FdwCatalogSource(sf), clients)
    report = await sync.run()
    await clients.aclose()

    by_node = {n.node: n for n in report.nodes}
    assert by_node["fdwtest1"].error is None and by_node["fdwtest1"].entries == 2
    assert by_node["fdwtest2"].error is None and by_node["fdwtest2"].entries == 2
    assert by_node["jf1"].error is not None  # unreadable node is isolated, not fatal

    async with engine.connect() as conn:
        copies = dict(
            (
                await conn.execute(
                    text("SELECT t.name, count(*) FROM copy c JOIN title t ON t.id = c.title_id "
                         "GROUP BY t.name")
                )
            ).all()
        )
    assert copies == {"Big Buck Bunny": 2, "Sintel": 1, "Spring": 1}

    # Re-running is idempotent; a file that disappears is marked, not deleted.
    write_node_db(Path(FIXTURE_DIR or "") / "fdwtest2.db", [SPRING])
    clients = NodeClients()
    await CatalogSync(engine, sf, FdwCatalogSource(sf), clients).run()
    await clients.aclose()
    async with engine.connect() as conn:
        present = (
            await conn.execute(
                text("SELECT count(*) FILTER (WHERE is_present), count(*) FROM copy")
            )
        ).one()
    # fdwtest1: BBB + Sintel; fdwtest2: the new Spring item. The two old fdwtest2
    # copies are kept (rental history) but marked as no longer present.
    assert tuple(present) == (3, 5)
