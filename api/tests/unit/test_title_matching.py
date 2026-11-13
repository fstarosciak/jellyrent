import uuid
from decimal import Decimal

import pytest

from app.catalog_sources import CatalogEntry
from app.catalog_sync import TitleIndex, TitleRecord, merge_title, normalize_title
from app.models import MediaInfo, ProviderIds


def entry(
    name: str = "Sintel",
    year: int | None = 2010,
    imdb: str | None = None,
    tmdb: str | None = None,
    genres: tuple[str, ...] = (),
    rating: Decimal | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        jf_item_id=uuid.uuid4(),
        name=name,
        year=year,
        path=f"/media/{name}.mp4",
        imdb=imdb,
        tmdb=tmdb,
        genres=genres,
        tags=(),
        rating=rating,
        media=MediaInfo(),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Big Buck Bunny", "big buck bunny"),
        ("  Tears   of Steel ", "tears of steel"),
        ("Amélie", "amelie"),
        ("Spider-Man: No Way Home", "spider man no way home"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_matches_by_imdb_first() -> None:
    title = TitleRecord(id=1, name="Completely Different", year=1999, imdb="tt1727587", tmdb=None)
    index = TitleIndex([title])
    assert index.match(entry(imdb="tt1727587")) is title


def test_matches_by_tmdb_when_imdb_unknown() -> None:
    title = TitleRecord(id=1, name="Sintel", year=2010, imdb=None, tmdb="45745")
    index = TitleIndex([title])
    assert index.match(entry(name="Sintel (Director's cut)", tmdb="45745")) is title


def test_falls_back_to_normalised_name_and_year() -> None:
    title = TitleRecord(id=7, name="Tears of Steel", year=2012, imdb=None, tmdb=None)
    index = TitleIndex([title])
    assert index.match(entry(name="tears of steel", year=2012)) is title
    assert index.match(entry(name="Tears of Steel", year=2013)) is None


def test_fallback_refuses_conflicting_external_ids() -> None:
    """Same name and year but different IMDb ids are different films."""
    title = TitleRecord(id=1, name="Spring", year=2019, imdb="tt0000001", tmdb=None)
    index = TitleIndex([title])
    assert index.match(entry(name="Spring", year=2019, imdb="tt9999999")) is None
    # ...but an entry without ids may still match by name.
    assert index.match(entry(name="Spring", year=2019)) is title


def test_tmdb_match_refuses_conflicting_imdb() -> None:
    title = TitleRecord(id=1, name="X", year=2000, imdb="tt1", tmdb="42")
    index = TitleIndex([title])
    assert index.match(entry(name="Y", year=2001, imdb="tt2", tmdb="42")) is None


def test_added_titles_become_matchable() -> None:
    index = TitleIndex()
    record = TitleRecord(id=3, name="Spring", year=2019, imdb=None, tmdb=None)
    index.add(record)
    assert index.match(entry(name="Spring", year=2019)) is record


def test_merge_fills_gaps_and_unions_genres() -> None:
    title = TitleRecord(
        id=1, name="Sintel", year=None, imdb=None, tmdb="45745", genres=("Animation",)
    )
    changes = merge_title(
        title,
        entry(imdb="tt1727587", year=2010, genres=("Fantasy", "Animation"), rating=Decimal("7.4")),
    )
    assert changes == {
        "providers": ProviderIds(imdb="tt1727587", tmdb="45745"),
        "year": 2010,
        "rating": Decimal("7.4"),
        "genres": ["Animation", "Fantasy"],
    }


def test_merge_never_overwrites_existing_data() -> None:
    title = TitleRecord(
        id=1, name="Sintel", year=2010, imdb="tt1727587", tmdb="45745",
        genres=("Animation",), rating=Decimal("7.0"),
    )
    changes = merge_title(title, entry(imdb="tt0000000", year=2011, rating=Decimal("9.9")))
    assert changes == {}
