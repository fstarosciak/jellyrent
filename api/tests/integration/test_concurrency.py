"""Concurrency of rent_title() under real parallel transactions (spec scenario 5).

Each worker thread has its own connection; a barrier releases them at the same
moment so the transactions really overlap.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from .conftest import PG_DSN, Seed

WORKERS = 50


def run_parallel(n: int, call: Callable[[psycopg.Connection, int], None]) -> Counter[str]:
    """Run `call(conn, i)` in n threads at once; count outcomes by SQLSTATE."""
    barrier = threading.Barrier(n)
    connections = [psycopg.connect(PG_DSN, autocommit=False) for _ in range(n)]

    def worker(i: int) -> str:
        conn = connections[i]
        barrier.wait()
        try:
            call(conn, i)
            conn.commit()
            return "ok"
        except psycopg.Error as exc:
            conn.rollback()
            return exc.sqlstate or type(exc).__name__

    try:
        with ThreadPoolExecutor(max_workers=n) as pool:
            return Counter(pool.map(worker, range(n)))
    finally:
        for conn in connections:
            conn.close()


def live_rentals(title_id: int) -> list[tuple[int, int]]:
    with psycopg.connect(PG_DSN) as conn:
        return conn.execute(
            "SELECT r.copy_id, r.user_id FROM rental r JOIN copy c ON c.id = r.copy_id "
            "WHERE c.title_id = %s AND r.status IN ('PENDING', 'ACTIVE', 'RETURNING')",
            (title_id,),
        ).fetchall()


async def test_fifty_parallel_rentals_of_two_copies(seed: Seed) -> None:
    title = await seed.title("Sintel")
    await seed.copy(title, 1)
    await seed.copy(title, 2)
    users = [await seed.user(f"user{i:02d}") for i in range(WORKERS)]

    def rent(conn: psycopg.Connection, i: int) -> None:
        conn.execute("SELECT id FROM rent_title(%s, %s)", (users[i], title))

    outcomes = run_parallel(WORKERS, rent)

    assert outcomes == Counter({"ok": 2, "JR002": WORKERS - 2})  # no 23505 surfaced
    rentals = live_rentals(title)
    assert len({copy_id for copy_id, _ in rentals}) == 2  # two different copies


async def test_rental_limit_cannot_be_raced(seed: Seed) -> None:
    """One user fires 10 rentals of different titles at once; limit is 3."""
    user = await seed.user("greedy", max_rentals=3)
    titles = []
    for i in range(10):
        titles.append(await seed.title(f"Title {i}"))
        await seed.copy(titles[-1], 1)

    def rent(conn: psycopg.Connection, i: int) -> None:
        conn.execute("SELECT id FROM rent_title(%s, %s)", (user, titles[i]))

    outcomes = run_parallel(10, rent)
    assert outcomes == Counter({"ok": 3, "JR001": 7})


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"])
async def test_isolation_levels_never_double_rent(seed: Seed, isolation: str) -> None:
    """Comparison for the report: correctness holds at every level.

    READ COMMITTED + row locks yields clean outcomes (success or JR002).
    Under REPEATABLE READ / SERIALIZABLE the same contention additionally
    produces serialization failures (40001) that a client would have to retry.
    """
    title = await seed.title("Big Buck Bunny", year=2008)
    await seed.copy(title, 1)
    await seed.copy(title, 2)
    users = [await seed.user(f"iso{i:02d}") for i in range(20)]

    def rent(conn: psycopg.Connection, i: int) -> None:
        conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
        conn.execute("SELECT id FROM rent_title(%s, %s)", (users[i], title))

    outcomes = run_parallel(20, rent)
    print(f"\n{isolation}: {dict(outcomes)}")  # noqa: T201 (shown with -s, used in the report)

    assert outcomes["ok"] <= 2
    assert set(outcomes) <= {"ok", "JR002", "40001"}
    assert len(live_rentals(title)) == outcomes["ok"]
    if isolation == "READ COMMITTED":
        assert outcomes == Counter({"ok": 2, "JR002": 18})
