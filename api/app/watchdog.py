"""Background worker: heartbeat, session guard, expiry, saga repair, sync.

Run with ``python -m app.watchdog``. Several replicas may run: one becomes the
leader by holding a session-level advisory lock on a dedicated connection and
runs the jobs; the others stay on standby. If the leader's connection dies,
PostgreSQL releases the lock and a standby takes over.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings
from app.errors import SyncInProgress
from app.locks import lock_key, try_session_lock
from app.logging_setup import configure_logging
from app.services import Services

log = logging.getLogger("app.watchdog")

LEADER_LOCK = lock_key("watchdog-leader")


@dataclass(slots=True)
class PeriodicJob:
    """Runs `func` every `interval` seconds; a failing run never kills the loop."""

    name: str
    interval: float
    func: Callable[[], Awaitable[object]]

    @property
    def timeout(self) -> float:
        return max(60.0, self.interval * 10)

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                result = await asyncio.wait_for(self.func(), timeout=self.timeout)
                if result:
                    log.debug("job %s: %s", self.name, result)
            except TimeoutError:
                log.error("job %s timed out after %.0fs", self.name, self.timeout)
            except Exception:
                log.exception("job %s failed", self.name)
            await asyncio.sleep(max(0.0, self.interval - (loop.time() - started)))


def build_jobs(services: Services) -> list[PeriodicJob]:
    s = services.settings

    async def reconcile_all() -> int:
        async with services.session_factory() as session:
            node_ids = (
                await session.scalars(text("SELECT id FROM node WHERE status = 'ONLINE'"))
            ).all()
        for node_id in node_ids:
            await services.access.reconcile_node(node_id)
        return len(node_ids)

    async def sync_catalog() -> object:
        try:
            return await services.catalog.run()
        except SyncInProgress:
            return "skipped: sync already running"

    return [
        PeriodicJob("heartbeat", s.heartbeat_interval, services.monitor.heartbeat),
        PeriodicJob("session-guard", s.session_guard_interval, services.guard.run),
        PeriodicJob("expiry", s.expiry_interval, services.saga.expire_overdue),
        PeriodicJob("saga-repair", s.repair_interval, services.saga.repair),
        PeriodicJob("reconcile-access", s.reconcile_interval, reconcile_all),
        PeriodicJob("catalog-sync", s.catalog_sync_interval, sync_catalog),
    ]


class Worker:
    def __init__(self, settings: Settings, services: Services) -> None:
        self._settings = settings
        self._services = services
        # Dedicated, unpooled engine: closing the connection must really close
        # it, so that the session-level leader lock is released.
        self._lock_engine = create_async_engine(settings.database_url, poolclass=NullPool)
        self._heartbeat_file = Path(settings.worker_heartbeat_file)

    def _touch(self) -> None:
        with contextlib.suppress(OSError):
            self._heartbeat_file.touch()

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self._lead_or_wait(stop)
                except Exception:
                    log.exception("leadership lost")
                self._touch()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)
        finally:
            await self._lock_engine.dispose()

    async def _lead_or_wait(self, stop: asyncio.Event) -> None:
        async with self._lock_engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            if not await try_session_lock(conn, LEADER_LOCK):
                log.debug("standby: another worker is the leader")
                return
            log.info("this worker is the leader, starting jobs")
            jobs = build_jobs(self._services)
            tasks = [asyncio.create_task(j.run_forever(), name=j.name) for j in jobs]
            try:
                while not stop.is_set():
                    self._touch()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=5)
                    await conn.execute(text("SELECT 1"))  # raises if the lock connection died
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                log.info("jobs stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    services = Services.build(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker starting")
    try:
        await Worker(settings, services).run(stop)
    finally:
        await services.aclose()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
