"""Async client for the Jellyfin REST API (pinned server version, see README).

Design notes
* Objects that are read, modified and written back (user policies, item
  DTOs) are kept as plain dicts: Jellyfin replaces the whole object on write
  and clears fields that are missing, so unknown fields must round-trip.
* Read-only projections (sessions) are parsed into small frozen dataclasses.
* Transport problems, timeouts and 5xx map to JellyfinUnavailable (retryable);
  4xx map to JellyfinRequestError (a bug or a rejected request).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

CLIENT_NAME = "JellyRent"
CLIENT_VERSION = "0.1.0"
DEVICE_ID = "jellyrent-broker"
MOVIE_FIELDS = "ProviderIds,Path,Genres,Tags,MediaStreams,Width,Height"


class JellyfinError(Exception):
    def __init__(self, node: str, message: str) -> None:
        super().__init__(f"[{node}] {message}")
        self.node = node


class JellyfinUnavailable(JellyfinError):
    """Node unreachable, timed out or failing (5xx). Safe to retry later."""


class JellyfinRequestError(JellyfinError):
    """The node rejected the request (4xx)."""

    def __init__(self, node: str, status_code: int, message: str) -> None:
        super().__init__(node, f"HTTP {status_code}: {message}")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SessionInfo:
    id: str
    user_id: uuid.UUID | None
    user_name: str | None
    device_name: str | None
    client: str | None
    now_playing_item_id: uuid.UUID | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SessionInfo:
        playing = data.get("NowPlayingItem") or {}
        return cls(
            id=str(data["Id"]),
            user_id=_parse_uuid(data.get("UserId")),
            user_name=data.get("UserName"),
            device_name=data.get("DeviceName"),
            client=data.get("Client"),
            now_playing_item_id=_parse_uuid(playing.get("Id")),
        )


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return None if parsed.int == 0 else parsed


def _jf_id(value: uuid.UUID | str) -> str:
    """Jellyfin's canonical id format: 32 hex digits, no dashes."""
    return value.hex if isinstance(value, uuid.UUID) else uuid.UUID(value).hex


def authorization_header(token: str | None) -> str:
    parts = [
        f'Client="{CLIENT_NAME}"',
        'Device="broker"',
        f'DeviceId="{DEVICE_ID}"',
        f'Version="{CLIENT_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        node: str = "jellyfin",
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.node = node
        self.base_url = base_url
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": authorization_header(token), "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
            transport=transport or httpx.AsyncHTTPTransport(retries=1),  # retries connect errors
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> JellyfinClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise JellyfinUnavailable(self.node, f"timeout on {method} {path}") from exc
        except httpx.TransportError as exc:
            raise JellyfinUnavailable(
                self.node, f"{type(exc).__name__} on {method} {path}"
            ) from exc
        if response.status_code >= 500:
            raise JellyfinUnavailable(self.node, f"HTTP {response.status_code} on {method} {path}")
        if response.status_code >= 400:
            raise JellyfinRequestError(self.node, response.status_code, response.text[:200])
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        return response.json() if response.content else None

    # ---------------------------------------------------------------- system
    async def ping(self) -> dict[str, Any]:
        """GET /System/Info/Public - no authentication required."""
        result: dict[str, Any] = await self._json("GET", "/System/Info/Public")
        return result

    async def system_info(self) -> dict[str, Any]:
        """GET /System/Info - requires a valid token (used to validate API keys)."""
        result: dict[str, Any] = await self._json("GET", "/System/Info")
        return result

    # ----------------------------------------------------------------- users
    async def get_user(self, user_id: uuid.UUID | str) -> dict[str, Any]:
        result: dict[str, Any] = await self._json("GET", f"/Users/{_jf_id(user_id)}")
        return result

    async def find_user_by_name(self, name: str) -> dict[str, Any] | None:
        users: list[dict[str, Any]] = await self._json("GET", "/Users")
        lowered = name.casefold()
        return next((u for u in users if str(u.get("Name", "")).casefold() == lowered), None)

    async def create_user(self, name: str, password: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._json(
            "POST", "/Users/New", json={"Name": name, "Password": password}
        )
        return result

    async def set_password(self, user_id: uuid.UUID | str, new_password: str) -> None:
        await self._request(
            "POST",
            "/Users/Password",
            params={"userId": _jf_id(user_id)},
            json={"NewPw": new_password, "ResetPassword": False},
        )

    async def update_policy(self, user_id: uuid.UUID | str, policy: dict[str, Any]) -> None:
        await self._request("POST", f"/Users/{_jf_id(user_id)}/Policy", json=policy)

    # ----------------------------------------------------------------- items
    async def get_item(self, item_id: uuid.UUID | str) -> dict[str, Any]:
        result: dict[str, Any] = await self._json("GET", f"/Items/{_jf_id(item_id)}")
        return result

    async def update_item(self, item_id: uuid.UUID | str, dto: dict[str, Any]) -> None:
        await self._request("POST", f"/Items/{_jf_id(item_id)}", json=dto)

    async def ensure_item_tag(self, item_id: uuid.UUID | str, tag: str) -> bool:
        """Add `tag` to the item and lock the Tags field. Returns True if changed.

        UpdateItem replaces the item with the posted DTO, so we post back the
        complete DTO we just read. Locking "Tags" stops metadata refreshes from
        removing the tag again.
        """
        dto = await self.get_item(item_id)
        tags: list[str] = list(dto.get("Tags") or [])
        locked: list[str] = list(dto.get("LockedFields") or [])
        if tag in tags and "Tags" in locked:
            return False
        if tag not in tags:
            tags.append(tag)
        if "Tags" not in locked:
            locked.append("Tags")
        await self.update_item(item_id, {**dto, "Tags": tags, "LockedFields": locked})
        return True

    async def list_movies(self) -> list[dict[str, Any]]:
        data = await self._json(
            "GET",
            "/Items",
            params={
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": MOVIE_FIELDS,
                "EnableImages": "false",
                "EnableUserData": "false",
            },
        )
        items: list[dict[str, Any]] = data.get("Items", []) if data else []
        return items

    # -------------------------------------------------------------- sessions
    async def get_sessions(self, active_within_seconds: int | None = None) -> list[SessionInfo]:
        params = {"activeWithinSeconds": active_within_seconds} if active_within_seconds else None
        data: list[dict[str, Any]] = await self._json("GET", "/Sessions", params=params)
        return [SessionInfo.from_api(s) for s in data or []]

    async def stop_session(self, session_id: str) -> None:
        await self._request("POST", f"/Sessions/{session_id}/Playing/Stop")

    async def send_message(self, session_id: str, header: str, text: str) -> None:
        await self._request(
            "POST",
            f"/Sessions/{session_id}/Message",
            json={"Header": header, "Text": text, "TimeoutMs": 8000},
        )

    # ------------------------------------------------- setup (bootstrap only)
    async def startup_complete_wizard(
        self, *, server_name: str, admin_user: str, admin_password: str
    ) -> None:
        """Runs the first-start wizard non-interactively."""
        await self._request("GET", "/Startup/User")  # initialises the first user
        await self._request(
            "POST",
            "/Startup/Configuration",
            json={
                "ServerName": server_name,
                "UICulture": "en-US",
                "MetadataCountryCode": "US",
                "PreferredMetadataLanguage": "en",
            },
        )
        await self._request(
            "POST", "/Startup/User", json={"Name": admin_user, "Password": admin_password}
        )
        await self._request("POST", "/Startup/RemoteAccess", json={"EnableRemoteAccess": True})
        await self._request("POST", "/Startup/Complete")

    async def authenticate_by_name(self, username: str, password: str) -> str:
        data = await self._json(
            "POST", "/Users/AuthenticateByName", json={"Username": username, "Pw": password}
        )
        return str(data["AccessToken"])

    async def list_virtual_folders(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._json("GET", "/Library/VirtualFolders")
        return result

    async def add_movie_library(self, name: str, path: str) -> None:
        await self._request(
            "POST",
            "/Library/VirtualFolders",
            params={
                "name": name,
                "collectionType": "movies",
                "paths": path,
                "refreshLibrary": "true",
            },
            json={"LibraryOptions": {"EnableRealtimeMonitor": True}},
        )

    async def refresh_library(self) -> None:
        await self._request("POST", "/Library/Refresh")

    async def list_api_keys(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/Auth/Keys")
        items: list[dict[str, Any]] = data.get("Items", []) if data else []
        return items

    async def create_api_key(self, app_name: str) -> None:
        await self._request("POST", "/Auth/Keys", params={"app": app_name})
