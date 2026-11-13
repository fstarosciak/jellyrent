"""FastAPI dependencies: services, DB sessions, authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Forbidden, Unauthorized
from app.models import AppUser
from app.security import TokenClaims, decode_access_token
from app.services import Services

_bearer = HTTPBearer(auto_error=False)


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_session(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    async with services.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_claims(
    services: ServicesDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TokenClaims:
    if credentials is None:
        raise Unauthorized()
    return decode_access_token(
        credentials.credentials, secret=services.settings.jwt_secret.get_secret_value()
    )


ClaimsDep = Annotated[TokenClaims, Depends(get_claims)]


async def get_current_user(claims: ClaimsDep, session: SessionDep) -> AppUser:
    user = await session.get(AppUser, claims.user_id)
    if user is None:
        raise Unauthorized("account no longer exists")
    return user


CurrentUser = Annotated[AppUser, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> AppUser:
    if not user.is_admin:
        raise Forbidden("administrator role required")
    return user


AdminUser = Annotated[AppUser, Depends(require_admin)]
