"""Mapping helpers: DB errors, composite types, REST catalog entries."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from app.catalog_sources import entry_from_rest
from app.errors import AlreadyRented, NoCopyAvailable, RentalLimitReached, translate_db_error
from app.models import Copy, MediaInfo, ProviderIds, Title

DIALECT = postgresql.psycopg.dialect()  # type: ignore[attr-defined]


def db_error(sqlstate: str, detail: str | None = None) -> DBAPIError:
    orig = SimpleNamespace(sqlstate=sqlstate, diag=SimpleNamespace(message_detail=detail))
    return DBAPIError("SELECT ...", {}, orig)  # type: ignore[arg-type]


def test_custom_sqlstates_map_to_domain_errors() -> None:
    assert isinstance(translate_db_error(db_error("JR001", "limit=3")), RentalLimitReached)
    assert isinstance(translate_db_error(db_error("JR002")), NoCopyAvailable)
    assert isinstance(translate_db_error(db_error("JR003")), AlreadyRented)
    error = translate_db_error(db_error("JR001", "limit=3"))
    assert error is not None and error.detail == "limit=3"


def test_unknown_sqlstate_is_not_translated() -> None:
    assert translate_db_error(db_error("40001")) is None


def test_composite_type_is_read_via_to_jsonb() -> None:
    sql = str(select(Title.providers).compile(dialect=DIALECT))
    assert "to_jsonb(title.providers)" in sql


def test_composite_type_is_written_via_jsonb_populate_record() -> None:
    stmt = pg_insert(Title).values(name="Sintel", providers=ProviderIds(imdb="tt1727587"))
    compiled = stmt.compile(dialect=DIALECT)
    assert "jsonb_populate_record(CAST(NULL AS provider_ids)" in str(compiled)
    processor = compiled._bind_processors["providers"]
    assert processor(ProviderIds(imdb="tt1727587")) == '{"imdb": "tt1727587", "tmdb": null}'


def test_composite_result_processor_builds_dataclass() -> None:
    column_type = Copy.__table__.c.media.type
    process = column_type.result_processor(DIALECT, None)
    assert process({"container": "mp4", "width": 1920, "unknown": 1}) == MediaInfo(
        container="mp4", width=1920
    )
    assert process(None) is None


def test_row_methods_render_whole_row_reference() -> None:
    from sqlalchemy.orm import undefer

    sql = str(select(Copy).options(undefer(Copy.is_available)).compile(dialect=DIALECT))  # type: ignore[attr-defined]
    assert "is_available(copy)" in sql


def test_rest_item_to_catalog_entry() -> None:
    item_id = uuid.uuid4()
    entry = entry_from_rest(
        {
            "Id": item_id.hex,
            "Name": "Big Buck Bunny",
            "ProductionYear": 2008,
            "Path": "/media/Big Buck Bunny (2008)/Big Buck Bunny (2008).MP4",
            "ProviderIds": {"Imdb": "tt1254207", "Tmdb": "10378", "Tvdb": ""},
            "Genres": ["Animation"],
            "Tags": ["rent-1"],
            "CommunityRating": 6.54,
            "RunTimeTicks": 5_960_000_000,
            "Width": 1280,
            "Height": 720,
            "MediaStreams": [{"Type": "Audio", "Codec": "aac"}, {"Type": "Video", "Codec": "h264"}],
        }
    )
    assert entry.jf_item_id == item_id
    assert (entry.imdb, entry.tmdb) == ("tt1254207", "10378")
    assert entry.rating == Decimal("6.5")
    assert entry.media == MediaInfo(
        container="mp4", video_codec="h264", width=1280, height=720, runtime_sec=596
    )
    assert entry.tags == ("rent-1",)
