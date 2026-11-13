import json
import uuid

import httpx
import pytest
import respx

from app.jellyfin_client import (
    JellyfinClient,
    JellyfinRequestError,
    JellyfinUnavailable,
    SessionInfo,
)

BASE = "http://jf.test"
ITEM = uuid.UUID("5a1b0c9e-1f2d-4c3b-8a7e-6d5c4b3a2f10")


@pytest.fixture
async def client() -> JellyfinClient:
    return JellyfinClient(BASE, "secret-key", node="jf1", timeout=1.0, transport=httpx.AsyncHTTPTransport())


@respx.mock
async def test_sends_mediabrowser_authorization(client: JellyfinClient) -> None:
    route = respx.get(f"{BASE}/System/Info/Public").mock(
        return_value=httpx.Response(200, json={"Version": "12.4.0"})
    )
    assert (await client.ping())["Version"] == "12.4.0"
    header = route.calls.last.request.headers["Authorization"]
    assert header.startswith("MediaBrowser ")
    assert 'Token="secret-key"' in header
    assert 'Client="JellyRent"' in header


@respx.mock
async def test_5xx_and_transport_errors_are_retryable(client: JellyfinClient) -> None:
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(503))
    with pytest.raises(JellyfinUnavailable):
        await client.get_sessions()

    respx.get(f"{BASE}/Sessions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(JellyfinUnavailable):
        await client.get_sessions()

    respx.get(f"{BASE}/Sessions").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(JellyfinUnavailable):
        await client.get_sessions()


@respx.mock
async def test_4xx_is_a_request_error(client: JellyfinClient) -> None:
    respx.post(f"{BASE}/Sessions/abc/Playing/Stop").mock(return_value=httpx.Response(404))
    with pytest.raises(JellyfinRequestError) as excinfo:
        await client.stop_session("abc")
    assert excinfo.value.status_code == 404


@respx.mock
async def test_ensure_item_tag_posts_back_the_full_dto(client: JellyfinClient) -> None:
    dto = {"Id": ITEM.hex, "Name": "Sintel", "Overview": "A girl and a dragon", "Tags": ["x"]}
    respx.get(f"{BASE}/Items/{ITEM.hex}").mock(return_value=httpx.Response(200, json=dto))
    update = respx.post(f"{BASE}/Items/{ITEM.hex}").mock(return_value=httpx.Response(204))

    assert await client.ensure_item_tag(ITEM, "rent-7") is True
    sent = json.loads(update.calls.last.request.content)
    # Fields we did not touch must round-trip, otherwise Jellyfin clears them.
    assert sent["Overview"] == "A girl and a dragon"
    assert sent["Tags"] == ["x", "rent-7"]
    assert "Tags" in sent["LockedFields"]


@respx.mock
async def test_ensure_item_tag_is_a_noop_when_already_tagged(client: JellyfinClient) -> None:
    dto = {"Id": ITEM.hex, "Name": "Sintel", "Tags": ["rent-7"], "LockedFields": ["Tags"]}
    respx.get(f"{BASE}/Items/{ITEM.hex}").mock(return_value=httpx.Response(200, json=dto))
    update = respx.post(f"{BASE}/Items/{ITEM.hex}")
    assert await client.ensure_item_tag(ITEM, "rent-7") is False
    assert not update.called


@respx.mock
async def test_sessions_are_parsed(client: JellyfinClient) -> None:
    user = uuid.uuid4()
    respx.get(f"{BASE}/Sessions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"Id": "s1", "UserId": user.hex, "UserName": "alice", "DeviceName": "TV",
                 "Client": "Jellyfin Web", "NowPlayingItem": {"Id": ITEM.hex}},
                {"Id": "s2", "UserId": "00000000000000000000000000000000"},
            ],
        )
    )
    s1, s2 = await client.get_sessions()
    assert s1 == SessionInfo("s1", user, "alice", "TV", "Jellyfin Web", ITEM)
    assert s2.user_id is None and s2.now_playing_item_id is None


@respx.mock
async def test_find_user_by_name_is_case_insensitive(client: JellyfinClient) -> None:
    respx.get(f"{BASE}/Users").mock(
        return_value=httpx.Response(200, json=[{"Name": "Alice", "Id": uuid.uuid4().hex}])
    )
    assert (await client.find_user_by_name("alice")) is not None
    assert (await client.find_user_by_name("bob")) is None
