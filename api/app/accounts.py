"""User accounts: JellyRent users and their accounts on the Jellyfin nodes.

A user signs in to Jellyfin with the same username and password as to
JellyRent. The plaintext password exists only during register/login (it is
never stored; JellyRent keeps an Argon2 hash), so node accounts are created
at registration and, for nodes that were offline then, at the next login.

New node accounts are locked down immediately: AllowedTags = [sentinel], so
the account sees nothing until a rental grants a copy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.access import apply_access_policy
from app.db import SessionFactory
from app.errors import InvalidInput, Unauthorized, UsernameTaken
from app.jellyfin_client import JellyfinError
from app.models import AppUser, NodeAccount
from app.node_registry import NodeClients, NodeRef, load_nodes
from app.security import hash_password, needs_rehash, verify_password

log = logging.getLogger(__name__)


class ProvisioningRefused(JellyfinError):
    """The node has a conflicting account we must not take over."""


@dataclass(slots=True)
class ProvisionReport:
    provisioned: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class AccountService:
    def __init__(
        self,
        session_factory: SessionFactory,
        clients: NodeClients,
        *,
        sentinel_tag: str,
        reserved_usernames: frozenset[str] = frozenset(),
    ) -> None:
        self._sf = session_factory
        self._clients = clients
        self._sentinel = sentinel_tag
        self._reserved = {u.casefold() for u in reserved_usernames}

    # ------------------------------------------------------------ app users
    async def register(self, username: str, password: str) -> tuple[AppUser, ProvisionReport]:
        if username.casefold() in self._reserved:
            raise UsernameTaken()
        async with self._sf() as session:
            user = AppUser(username=username, password_hash=hash_password(password))
            session.add(user)
            try:
                await session.commit()
            except IntegrityError as exc:
                sqlstate = getattr(exc.orig, "sqlstate", None)
                if sqlstate == "23505":
                    raise UsernameTaken() from exc
                raise InvalidInput("invalid username") from exc
        report = await self.ensure_node_accounts(user.id, username, password)
        return user, report

    async def authenticate(self, username: str, password: str) -> AppUser:
        async with self._sf() as session:
            user = await session.scalar(select(AppUser).where(AppUser.username == username))
            # Always run the hash check, even for unknown users (timing safety).
            password_ok = verify_password(user.password_hash if user else None, password)
            if user is None or not password_ok:
                raise Unauthorized("invalid username or password")
            if needs_rehash(user.password_hash):
                user.password_hash = hash_password(password)
                await session.commit()
        return user

    async def ensure_admin(self, username: str, password: str) -> None:
        """Create the JellyRent administrator on first start (idempotent)."""
        async with self._sf() as session, session.begin():
            await session.execute(
                pg_insert(AppUser)
                .values(username=username, password_hash=hash_password(password), is_admin=True)
                .on_conflict_do_nothing(index_elements=["username"])
            )
            # An existing user with this name is promoted, never re-passworded.
            await session.execute(
                update(AppUser).where(AppUser.username == username).values(is_admin=True)
            )

    # --------------------------------------------------------- node accounts
    async def ensure_node_accounts(
        self, user_id: int, username: str, password: str
    ) -> ProvisionReport:
        """Create missing accounts on all ONLINE nodes (best effort, concurrent)."""
        async with self._sf() as session:
            nodes = [n for n in await load_nodes(session) if n.is_online and n.api_key]
            existing = set(
                (
                    await session.scalars(
                        select(NodeAccount.node_id).where(NodeAccount.user_id == user_id)
                    )
                ).all()
            )
        missing = [n for n in nodes if n.id not in existing]
        results = await asyncio.gather(
            *(self._provision(n, user_id, username, password) for n in missing),
            return_exceptions=True,
        )
        report = ProvisionReport()
        for node, outcome in zip(missing, results, strict=True):
            if isinstance(outcome, JellyfinError):
                report.failed[node.name] = str(outcome)
                log.warning("provisioning %s on %s failed: %s", username, node.name, outcome)
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                report.provisioned.append(node.name)
        return report

    async def _provision(self, node: NodeRef, user_id: int, username: str, password: str) -> None:
        client = self._clients.client_for(node)
        existing = await client.find_user_by_name(username)
        if existing is not None:
            # Adopt a leftover account (e.g. a crash between creating it and
            # recording it), but never an administrator.
            if (existing.get("Policy") or {}).get("IsAdministrator"):
                raise ProvisioningRefused(node.name, f"'{username}' is an administrator there")
            jf_user_id = uuid.UUID(existing["Id"])
            await client.set_password(jf_user_id, password)
        else:
            jf_user_id = uuid.UUID((await client.create_user(username, password))["Id"])

        user = await client.get_user(jf_user_id)
        await client.update_policy(
            jf_user_id, apply_access_policy(user.get("Policy") or {}, set(), self._sentinel)
        )

        async with self._sf() as session, session.begin():
            stmt = pg_insert(NodeAccount).values(
                user_id=user_id, node_id=node.id, jf_user_id=jf_user_id
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["user_id", "node_id"],
                    set_={"jf_user_id": stmt.excluded.jf_user_id},
                )
            )
        log.info("provisioned %s on %s", username, node.name)
