"""Composition root: builds the object graph shared by the API and the worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from app.access import AccessManager
from app.accounts import AccountService
from app.catalog_sources import (
    CatalogSource,
    FallbackCatalogSource,
    FdwCatalogSource,
    RestCatalogSource,
)
from app.catalog_sync import CatalogSync
from app.config import CatalogSourceKind, Settings
from app.db import SessionFactory, create_engine, create_session_factory
from app.node_registry import NodeClients
from app.nodes import NodeMonitor
from app.rentals import RentalSaga, SqlRentalStore
from app.session_guard import SessionGuard


def _catalog_source(
    kind: CatalogSourceKind, session_factory: SessionFactory, clients: NodeClients
) -> CatalogSource:
    fdw = FdwCatalogSource(session_factory)
    rest = RestCatalogSource(clients)
    match kind:
        case CatalogSourceKind.FDW:
            return fdw
        case CatalogSourceKind.REST:
            return rest
        case CatalogSourceKind.AUTO:
            return FallbackCatalogSource(fdw, rest)


@dataclass(slots=True)
class Services:
    settings: Settings
    engine: AsyncEngine
    session_factory: SessionFactory
    clients: NodeClients
    access: AccessManager
    rental_store: SqlRentalStore
    saga: RentalSaga
    accounts: AccountService
    catalog: CatalogSync
    monitor: NodeMonitor
    guard: SessionGuard

    @classmethod
    def build(cls, settings: Settings) -> Services:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        clients = NodeClients(timeout=settings.jellyfin_timeout_seconds)
        access = AccessManager(session_factory, clients, sentinel_tag=settings.access_sentinel_tag)
        store = SqlRentalStore(session_factory)
        return cls(
            settings=settings,
            engine=engine,
            session_factory=session_factory,
            clients=clients,
            access=access,
            rental_store=store,
            saga=RentalSaga(
                store,
                access,
                default_hours=settings.rental_default_hours,
                pending_timeout=timedelta(seconds=settings.pending_timeout_seconds),
            ),
            accounts=AccountService(
                session_factory,
                clients,
                sentinel_tag=settings.access_sentinel_tag,
                reserved_usernames=frozenset({settings.jellyfin_admin_username}),
            ),
            catalog=CatalogSync(
                engine,
                session_factory,
                _catalog_source(settings.catalog_source, session_factory, clients),
                clients,
            ),
            monitor=NodeMonitor(
                session_factory,
                clients,
                access,
                failure_threshold=settings.heartbeat_failure_threshold,
            ),
            guard=SessionGuard(session_factory, clients),
        )

    async def aclose(self) -> None:
        await self.clients.aclose()
        await self.engine.dispose()
