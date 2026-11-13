"""Fixtures for tests against the real central database (owner role)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import SessionFactory, create_session_factory

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
# Plain libpq DSN for the synchronous psycopg connections of the concurrency tests.
PG_DSN = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)

RESET_SQL = """
TRUNCATE rental_event, violation, playback_session, rental, node_account, copy, title, app_user
    RESTART IDENTITY CASCADE;
UPDATE node SET status = 'ONLINE', consecutive_failures = 0, last_error = NULL
 WHERE id IN (1, 2, 3);
"""


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(DATABASE_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        for statement in RESET_SQL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    yield eng
    await eng.dispose()


@pytest.fixture
def sf(engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(engine)


@dataclass
class Seed:
    """Helpers that create catalog data directly with SQL."""

    engine: AsyncEngine

    async def _scalar(self, sql: str, **params: object) -> int:
        async with self.engine.begin() as conn:
            return int((await conn.execute(text(sql), params)).scalar_one())

    async def title(self, name: str, *, year: int = 2010, imdb: str | None = None) -> int:
        return await self._scalar(
            "INSERT INTO title (name, year, providers) "
            "VALUES (:name, :year, ROW(:imdb, NULL)::provider_ids) RETURNING id",
            name=name, year=year, imdb=imdb,
        )

    async def copy(self, title_id: int, node_id: int) -> int:
        return await self._scalar(
            "INSERT INTO copy (title_id, node_id, jf_item_id) "
            "VALUES (:t, :n, :item) RETURNING id",
            t=title_id, n=node_id, item=uuid.uuid4(),
        )

    async def user(self, name: str, *, nodes: tuple[int, ...] = (1, 2, 3), max_rentals: int = 3) -> int:
        user_id = await self._scalar(
            "INSERT INTO app_user (username, password_hash, max_rentals) "
            "VALUES (:u, 'x', :m) RETURNING id",
            u=name, m=max_rentals,
        )
        async with self.engine.begin() as conn:
            for node_id in nodes:
                await conn.execute(
                    text("INSERT INTO node_account (user_id, node_id, jf_user_id) VALUES (:u, :n, :j)"),
                    {"u": user_id, "n": node_id, "j": uuid.uuid4()},
                )
        return user_id

    async def set_node_status(self, node_id: int, status: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text("UPDATE node SET status = CAST(:s AS node_status) WHERE id = :n"),
                {"s": status, "n": node_id},
            )


@pytest.fixture
def seed(engine: AsyncEngine) -> Seed:
    return Seed(engine)
