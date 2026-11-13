"""API request/response models (the public contract, decoupled from ORM)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.models import NodeStatus, RentalStatus, ReturnReason, ViolationReason

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{2,31}$"  # same rule as the app_user CHECK


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------- auth
class RegisterIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN, examples=["alice"])
    password: SecretStr = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    username: str = Field(max_length=32)
    password: SecretStr = Field(max_length=128)


class UserOut(ApiModel):
    id: int
    username: str
    is_admin: bool
    max_rentals: int


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 (OAuth2 token type, not a secret)
    expires_in: int
    user: UserOut


class RegisterOut(BaseModel):
    user: UserOut
    provisioned_nodes: list[str]
    failed_nodes: dict[str, str]


# -------------------------------------------------------------------- titles
class TitleOut(BaseModel):
    id: int
    name: str
    year: int | None
    genres: list[str]
    rating: Decimal | None
    imdb_url: str | None
    available: int
    total: int
    nodes: list[str]
    available_nodes: list[str]


class CopyOut(BaseModel):
    id: int
    node_id: int
    node: str
    node_status: NodeStatus
    available: bool
    resolution: str | None
    container: str | None
    video_codec: str | None
    runtime_sec: int | None


class TitleDetailOut(TitleOut):
    copies: list[CopyOut]


# ------------------------------------------------------------------- rentals
class RentIn(BaseModel):
    title_id: int = Field(gt=0)
    node_id: int | None = Field(default=None, gt=0, description="Pin a node (optional)")
    duration_hours: int | None = Field(default=None, ge=1, le=720)


class RentalOut(BaseModel):
    id: int
    status: RentalStatus
    return_reason: ReturnReason | None
    title_id: int
    title: str
    node_id: int
    node: str
    username: str | None = None
    rented_at: datetime
    due_at: datetime
    returned_at: datetime | None
    play_url: str | None = Field(description="Link to the copy on its node while access is granted")


# --------------------------------------------------------------------- admin
class NodeOut(ApiModel):
    id: int
    name: str
    status: NodeStatus
    public_url: str
    last_seen_at: datetime | None
    last_sync_at: datetime | None
    last_error: str | None
    provisioned: bool
    copies: int
    live_rentals: int


class NodePatch(BaseModel):
    maintenance: bool


class NodeSyncOut(BaseModel):
    node: str
    source: str
    entries: int
    titles_created: int
    copies_upserted: int
    copies_missing: int
    tags_applied: int
    error: str | None


class SyncOut(BaseModel):
    ok: bool
    nodes: list[NodeSyncOut]


class ViolationOut(BaseModel):
    id: int
    node: str
    copy_id: int
    title: str
    username: str | None
    jf_session_id: str
    reason: ViolationReason
    detail: str | None
    detected_at: datetime


class StatsOut(BaseModel):
    titles: int
    copies: int
    nodes_online: int
    nodes_total: int
    live_rentals: int
    rentals_total: int
    violations_24h: int
