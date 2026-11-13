"""One-shot, idempotent provisioning of the Jellyfin nodes.

Run with ``python -m app.bootstrap`` (the `bootstrap` service in compose). For
every node registered in the central database it:

  1. waits until the node answers,
  2. completes the first-start wizard (admin user) if needed,
  3. creates the "Movies" library on /media if missing (triggers a scan),
  4. obtains an API key for JellyRent and stores it in node.api_key.

Nodes whose stored API key already works are skipped, so re-running is cheap.
The API and the worker pick up new keys without a restart.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import select, update

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.jellyfin_client import JellyfinClient, JellyfinError
from app.logging_setup import configure_logging
from app.models import Node

log = logging.getLogger("app.bootstrap")

API_KEY_APP_NAME = "JellyRent"
LIBRARY_NAME = "Movies"
MEDIA_PATH = "/media"


async def wait_until_up(client: JellyfinClient, attempts: int = 60, delay: float = 5) -> dict:  # type: ignore[type-arg]
    for attempt in range(1, attempts + 1):
        try:
            return await client.ping()
        except JellyfinError as exc:
            log.info("waiting for %s (%d/%d): %s", client.node, attempt, attempts, exc)
            await asyncio.sleep(delay)
    raise TimeoutError(f"{client.node} did not come up")


async def key_works(base_url: str, name: str, api_key: str | None, http_timeout: float) -> bool:
    if not api_key:
        return False
    async with JellyfinClient(base_url, api_key, node=name, timeout=http_timeout) as client:
        try:
            await client.system_info()
        except JellyfinError:
            return False
    return True


async def provision_node(node: Node, settings: Settings) -> str:
    admin = settings.jellyfin_admin_username
    password = settings.jellyfin_admin_password.get_secret_value()
    timeout = settings.jellyfin_timeout_seconds * 4  # setup calls can be slow

    async with JellyfinClient(node.base_url, None, node=node.name, timeout=timeout) as anon:
        info = await wait_until_up(anon)
        if not info.get("StartupWizardCompleted"):
            log.info("%s: completing the startup wizard", node.name)
            await anon.startup_complete_wizard(
                server_name=node.name, admin_user=admin, admin_password=password
            )
        token = await anon.authenticate_by_name(admin, password)

    async with JellyfinClient(node.base_url, token, node=node.name, timeout=timeout) as client:
        folders = await client.list_virtual_folders()
        if not any(f.get("Name") == LIBRARY_NAME for f in folders):
            log.info("%s: creating library %r on %s", node.name, LIBRARY_NAME, MEDIA_PATH)
            await client.add_movie_library(LIBRARY_NAME, MEDIA_PATH)

        def find_key(keys: list[dict]) -> str | None:  # type: ignore[type-arg]
            return next(
                (k["AccessToken"] for k in keys if k.get("AppName") == API_KEY_APP_NAME), None
            )

        api_key = find_key(await client.list_api_keys())
        if api_key is None:
            await client.create_api_key(API_KEY_APP_NAME)
            api_key = find_key(await client.list_api_keys())
        if api_key is None:
            raise RuntimeError(f"{node.name}: API key was not created")
        return api_key


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    failures = 0
    try:
        async with session_factory() as session:
            nodes = (await session.scalars(select(Node).order_by(Node.id))).all()

        async def handle(node: Node) -> None:
            nonlocal failures
            if await key_works(
                node.base_url, node.name, node.api_key, settings.jellyfin_timeout_seconds
            ):
                log.info("%s: already provisioned", node.name)
                return
            try:
                api_key = await provision_node(node, settings)
            except (JellyfinError, TimeoutError, RuntimeError, KeyError) as exc:
                failures += 1
                log.error("%s: provisioning failed: %s", node.name, exc)
                return
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(Node).where(Node.id == node.id).values(api_key=api_key)
                )
            log.info("%s: provisioned, API key stored", node.name)

        await asyncio.gather(*(handle(n) for n in nodes))
    finally:
        await engine.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
