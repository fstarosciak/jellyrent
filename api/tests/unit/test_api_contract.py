"""HTTP contract checks that need no database (stub services)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class StubServices(SimpleNamespace):
    async def aclose(self) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(jwt_secret="s" * 40, admin_password=None)  # type: ignore[arg-type]
    stub = StubServices(settings=settings)
    with TestClient(create_app(settings, services=stub)) as test_client:  # type: ignore[arg-type]
        yield test_client


def test_liveness(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-request-id" in response.headers


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/titles"), ("GET", "/api/rentals/me"), ("GET", "/api/admin/nodes")],
)
def test_protected_endpoints_return_problem_details(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "UNAUTHORIZED"
    assert body["status"] == 401
    assert body["instance"] == path


def test_invalid_token_is_rejected(client: TestClient) -> None:
    response = client.get("/api/titles", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_registration_validates_username(client: TestClient) -> None:
    response = client.post("/api/auth/register", json={"username": "A!", "password": "longenough"})
    assert response.status_code == 422


def test_openapi_lists_the_rental_endpoints(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert {"/api/rentals", "/api/rentals/{rental_id}/return", "/api/admin/sync"} <= set(paths)


def test_weak_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(jwt_secret="short")  # type: ignore[arg-type]
