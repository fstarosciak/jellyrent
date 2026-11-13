"""Registration and login."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, status

from app.deps import CurrentUser, ServicesDep
from app.schemas import LoginIn, RegisterIn, RegisterOut, TokenOut, UserOut
from app.security import TokenClaims, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, services: ServicesDep) -> RegisterOut:
    """Create a JellyRent user and matching accounts on all ONLINE nodes."""
    user, report = await services.accounts.register(body.username, body.password.get_secret_value())
    return RegisterOut(
        user=UserOut.model_validate(user),
        provisioned_nodes=report.provisioned,
        failed_nodes=report.failed,
    )


@router.post("/login")
async def login(body: LoginIn, services: ServicesDep, background: BackgroundTasks) -> TokenOut:
    password = body.password.get_secret_value()
    user = await services.accounts.authenticate(body.username, password)
    # Nodes that were offline at registration get the account now (the
    # plaintext password is only available during login).
    background.add_task(services.accounts.ensure_node_accounts, user.id, user.username, password)
    settings = services.settings
    ttl = timedelta(minutes=settings.jwt_ttl_minutes)
    token = create_access_token(
        TokenClaims(user_id=user.id, username=user.username, is_admin=user.is_admin),
        secret=settings.jwt_secret.get_secret_value(),
        ttl=ttl,
    )
    return TokenOut(
        access_token=token, expires_in=int(ttl.total_seconds()), user=UserOut.model_validate(user)
    )


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
