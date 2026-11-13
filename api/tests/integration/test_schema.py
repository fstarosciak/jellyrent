"""Object-relational schema: inheritance, composite types, methods, ORM mapping."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import undefer

from app.db import SessionFactory
from app.models import Base, Copy, ProviderIds, Title, TitleAvailability

from .conftest import Seed


async def test_orm_mapping_matches_database_schema(engine: AsyncEngine) -> None:
    """The schema is SQL-first; every mapped column must exist in the database."""
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = await conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table.name},
            )
            db_columns = {r[0] for r in rows}
            assert db_columns, f"{table.name} missing in database"
            assert {c.name for c in table.columns} <= db_columns, table.name


async def test_media_item_is_abstract(engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError, match="media_item_is_abstract"):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO media_item (name) VALUES ('nope')"))


async def test_parent_table_reads_rows_of_children(engine: AsyncEngine, seed: Seed) -> None:
    await seed.title("Sintel")
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT tableoid::regclass::text, name FROM media_item"))
        ).all()
    assert [tuple(r) for r in rows] == [("title", "Sintel")]


async def test_composite_type_roundtrip_through_orm(sf: SessionFactory, engine: AsyncEngine) -> None:
    async with sf() as session:
        session.add(
            Title(name="Sintel", year=2010, providers=ProviderIds(imdb="tt1727587", tmdb="45745"),
                  genres=["Animation", "Fantasy"])
        )
        session.add(Title(name="No ids", year=2001, providers=None))
        await session.commit()

    async with sf() as session:
        titles = {t.name: t for t in (await session.scalars(select(Title))).all()}
    assert titles["Sintel"].providers == ProviderIds(imdb="tt1727587", tmdb="45745")
    assert titles["Sintel"].genres == ["Animation", "Fantasy"]
    assert titles["No ids"].providers is None

    async with engine.connect() as conn:
        imdb = (await conn.execute(text("SELECT (providers).imdb FROM title WHERE year = 2010"))).scalar()
        url = (await conn.execute(text("SELECT imdb_url(providers) FROM title WHERE year = 2010"))).scalar()
    assert imdb == "tt1727587"
    assert url == "https://www.imdb.com/title/tt1727587/"


async def test_imdb_ids_are_unique_across_titles(engine: AsyncEngine, seed: Seed) -> None:
    await seed.title("A", imdb="tt1")
    with pytest.raises(IntegrityError):
        await seed.title("B", imdb="tt1")


async def test_methods_on_copy_rows(sf: SessionFactory, seed: Seed) -> None:
    title_id = await seed.title("Sintel")
    copy_id = await seed.copy(title_id, node_id=1)
    async with sf() as session:
        copy = await session.scalar(
            select(Copy)
            .options(undefer(Copy.is_available), undefer(Copy.play_url), undefer(Copy.access_tag))  # type: ignore[attr-defined]
            .where(Copy.id == copy_id)
        )
    assert copy is not None
    assert copy.is_available is True  # type: ignore[attr-defined]
    assert copy.access_tag == f"rent-{copy_id}"  # type: ignore[attr-defined]
    assert copy.play_url == f"http://localhost:8096/web/#/details?id={copy.jf_item_id.hex}"  # type: ignore[attr-defined]


async def test_title_availability_view(sf: SessionFactory, seed: Seed) -> None:
    title_id = await seed.title("Big Buck Bunny", year=2008)
    for node_id in (1, 2, 3):
        await seed.copy(title_id, node_id)
    await seed.set_node_status(3, "OFFLINE")
    async with sf() as session:
        view = await session.get(TitleAvailability, title_id)
    assert view is not None
    assert (view.available, view.total) == (2, 3)
    assert view.nodes == ["jf1", "jf2", "jf3"]
    assert view.available_nodes == ["jf1", "jf2"]


async def test_global_library_is_a_union_of_node_fragments(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        definition = (
            await conn.execute(text("SELECT pg_get_viewdef('global_library'::regclass)"))
        ).scalar_one()
    for schema in ("node_jf1", "node_jf2", "node_jf3"):
        assert f"{schema}.movies" in definition
    assert definition.count("UNION ALL") >= 2
