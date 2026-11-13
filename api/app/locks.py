"""PostgreSQL advisory locks.

Used for coordination that does not map onto row locks:
  * serialising AllowedTags updates per (user, node),
  * making the catalog sync single-flight,
  * electing one active worker (leader) among replicas.

Keys are derived from a namespace + parts with a stable hash, so different
uses can never collide by accident.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession


def lock_key(namespace: str, *parts: object) -> int:
    """Stable signed 64-bit key for pg_advisory_* functions."""
    raw = ":".join([namespace, *map(str, parts)]).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


async def xact_lock(session: AsyncSession, key: int) -> None:
    """Block until the lock is held; released automatically at commit/rollback."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def try_xact_lock(session: AsyncSession, key: int) -> bool:
    result = await session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
    return bool(result.scalar_one())


async def try_session_lock(conn: AsyncConnection, key: int) -> bool:
    """Session-level lock: held for as long as the connection stays open."""
    result = await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
    return bool(result.scalar_one())
