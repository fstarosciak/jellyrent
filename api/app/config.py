"""Application settings (12-factor: everything comes from the environment)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CatalogSourceKind(StrEnum):
    """Where the catalog sync reads node libraries from."""

    FDW = "fdw"  # PostgreSQL foreign tables over the nodes' SQLite files
    REST = "rest"  # Jellyfin REST API
    AUTO = "auto"  # FDW, falling back to REST per node on failure


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- infrastructure -----------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://jellyrent_app:jellyrent_app@localhost:5432/jellyrent",
        description="SQLAlchemy URL of the central database (psycopg 3 driver).",
    )
    db_pool_size: int = 10
    log_level: str = "INFO"
    log_json: bool = False

    # --- security -----------------------------------------------------------
    jwt_secret: SecretStr = Field(default=SecretStr("change-me-in-production-please-32+chars"))
    jwt_ttl_minutes: int = 12 * 60
    admin_username: str = "admin"
    admin_password: SecretStr | None = None

    # --- rentals ------------------------------------------------------------
    rental_default_hours: int = Field(default=48, ge=1, le=720)
    pending_timeout_seconds: int = Field(default=120, ge=10)

    # --- Jellyfin -----------------------------------------------------------
    jellyfin_admin_username: str = "jellyrent-admin"
    jellyfin_admin_password: SecretStr = Field(default=SecretStr("change-me"))
    jellyfin_timeout_seconds: float = 5.0
    # Tag that no item ever carries. Jellyfin treats an EMPTY AllowedTags list
    # as "no restriction", so a managed user always has at least this tag,
    # which makes the default "sees nothing".
    access_sentinel_tag: str = "jellyrent-none"
    catalog_source: CatalogSourceKind = CatalogSourceKind.AUTO

    # --- worker intervals (seconds) -----------------------------------------
    heartbeat_interval: float = 10
    heartbeat_failure_threshold: int = 3
    session_guard_interval: float = 5
    expiry_interval: float = 60
    repair_interval: float = 60
    reconcile_interval: float = 300
    catalog_sync_interval: float = 300
    worker_heartbeat_file: str = "/tmp/jellyrent-worker.alive"  # noqa: S108 (container-local)

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_strength(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters long")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
