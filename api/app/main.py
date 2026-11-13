"""FastAPI application factory.

Run with: uvicorn --factory app.main:create_app
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import __doc__ as app_description
from app.config import Settings, get_settings
from app.errors import DomainError
from app.logging_setup import configure_logging
from app.routes import admin, auth, health, rentals, titles
from app.services import Services

log = logging.getLogger("app.http")


def problem(
    status: int, code: str, title: str, detail: str | None, request: Request
) -> JSONResponse:
    """RFC 9457 (formerly 7807) problem details."""
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"urn:jellyrent:error:{code}",
            "title": title,
            "status": status,
            "detail": detail or title,
            "code": code,
            "instance": request.url.path,
        },
    )


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    return problem(exc.http_status, exc.code, exc.title, exc.detail, request)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return problem(500, "INTERNAL_ERROR", "Internal server error", None, request)


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    if request.url.path not in ("/healthz", "/readyz"):
        log.info(
            "%s %s -> %d (%.1f ms)",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            extra={"request_id": request_id},
        )
    return response


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, json_output=settings.log_json)
        app.state.services = services or Services.build(settings)
        if settings.admin_password is not None:
            await app.state.services.accounts.ensure_admin(
                settings.admin_username, settings.admin_password.get_secret_value()
            )
        yield
        await app.state.services.aclose()

    app = FastAPI(
        title="JellyRent",
        version="0.1.0",
        description=(app_description or "").strip(),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.middleware("http")(request_context)

    app.include_router(health.router)
    for module in (auth, titles, rentals, admin):
        app.include_router(module.router, prefix="/api")
    return app
