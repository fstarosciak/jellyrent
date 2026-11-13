"""Catalog."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app import queries
from app.deps import CurrentUser, SessionDep
from app.schemas import TitleDetailOut, TitleOut

router = APIRouter(prefix="/titles", tags=["catalog"])


@router.get("")
async def list_titles(
    _: CurrentUser,  # authenticate before touching the database
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100, description="Name contains")] = None,
    genre: Annotated[str | None, Query(max_length=50)] = None,
    available_only: bool = False,
) -> list[TitleOut]:
    """Catalog with availability, e.g. 2/3 available on jf1, jf3."""
    return await queries.list_titles(session, q=q, genre=genre, available_only=available_only)


@router.get("/{title_id}")
async def get_title(title_id: int, _: CurrentUser, session: SessionDep) -> TitleDetailOut:
    """Title details with its copies per node."""
    return await queries.get_title(session, title_id)
