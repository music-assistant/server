"""Tests for the contents of a genre: searched, sorted and paged on the server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import MediaType, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Genre

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the names of the items mapped to the genre, in the order they are added to the library
NAMES = ("Zebra", "Apple", "Mango")


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store, which a genre mapping touches."""
    return music_mass_with_cache


async def _genre_with(mass: MusicAssistant, media_type: MediaType) -> str:
    """Add a genre with three library items of the given type mapped to it, return its id."""
    genre = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Test Genre", provider_mappings=set())
    )
    for index, name in enumerate(NAMES):
        if media_type == MediaType.TRACK:
            item: Any = await mass.music.tracks.add_item_to_library(
                create_track("qobuz_1", f"t{index}", name=name, isrc=f"ISRC{index}")
            )
        else:
            album = create_album("qobuz_1", f"a{index}", name=name)
            album.year = 2000 + index
            item = await mass.music.albums.add_item_to_library(album)
        await mass.music.genres.add_media_mapping(genre.item_id, media_type, item.item_id)
    return str(genre.item_id)


def _names(items: list[Any]) -> list[str]:
    return [item.name for item in items]


async def test_genre_tracks_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The tracks of a genre are searched, sorted and paged, by sort name by default."""
    genre_id = await _genre_with(mass, MediaType.TRACK)

    async def _tracks(**kwargs: Any) -> list[str]:
        return _names(await mass.music.genres.tracks(genre_id, **kwargs))

    assert await _tracks() == ["Apple", "Mango", "Zebra"]
    assert await _tracks(sort_field=SortField.NAME, sort_direction=SortDirection.DESC) == [
        "Zebra",
        "Mango",
        "Apple",
    ]
    # an outdated client's sort key still maps to the same sort
    assert await _tracks(order_by="name_desc") == ["Zebra", "Mango", "Apple"]
    assert await _tracks(search="mang") == ["Mango"]
    assert await _tracks(limit=1, offset=1) == ["Mango"]
    assert await _tracks(limit=0) == ["Apple", "Mango", "Zebra"]
    with pytest.raises(InvalidDataError):
        await _tracks(sort_field=SortField.YEAR)


async def test_genre_albums_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The albums of a genre are searched, sorted and paged, by sort name by default."""
    genre_id = await _genre_with(mass, MediaType.ALBUM)

    async def _albums(**kwargs: Any) -> list[str]:
        return _names(await mass.music.genres.albums(genre_id, **kwargs))

    assert await _albums() == ["Apple", "Mango", "Zebra"]
    # the newest first
    assert await _albums(sort_field=SortField.YEAR) == ["Mango", "Apple", "Zebra"]
    # an outdated client's sort key still maps to the same sort, in its own direction
    assert await _albums(order_by="year") == ["Zebra", "Apple", "Mango"]
    assert await _albums(search="zeb") == ["Zebra"]
    assert await _albums(limit=2, offset=2) == ["Zebra"]
    with pytest.raises(InvalidDataError):
        await _albums(sort_field=SortField.DURATION)


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ALBUM])
async def test_genre_random_seed_stays_stable_across_pages(
    mass: MusicAssistant, media_type: MediaType
) -> None:
    """Genre contents forward their seed to the shared SQL query."""
    genre_id = await _genre_with(mass, media_type)
    listing = (
        mass.music.genres.tracks if media_type == MediaType.TRACK else mass.music.genres.albums
    )
    whole = await listing(genre_id, sort_field=SortField.RANDOM, random_seed=0, limit=0)
    first = await listing(genre_id, sort_field=SortField.RANDOM, random_seed=0, limit=1)
    rest = await listing(genre_id, sort_field=SortField.RANDOM, random_seed=0, limit=2, offset=1)

    assert [item.item_id for item in first + rest] == [item.item_id for item in whole]


@pytest.mark.parametrize("field", [SortField.RANDOM, SortField.RANDOM_PLAY_COUNT])
@pytest.mark.parametrize("summary", [True, False])
async def test_seeded_genre_pages_exclude_hidden_rows_before_sampling(
    mass: MusicAssistant, field: SortField, summary: bool
) -> None:
    """Excluded genres must not consume slots in a seeded random page."""
    controller = mass.music.genres
    for index in range(6):
        await controller.add_item_to_library(
            Genre(
                item_id="0",
                provider="library",
                name=f"Paging Genre {index}",
                provider_mappings=set(),
            )
        )
    options: dict[str, Any] = {
        "search": "Paging Genre",
        "hide_empty": False,
        "sort_field": field,
        "random_seed": 0,
        "summary": summary,
        "_localized_fallback": False,
    }
    before = await controller.library_items(limit=0, **options)
    excluded = {before[0].item_id, before[-1].item_id}
    for item_id in excluded:
        await mass.music.database.execute(
            "UPDATE genres SET is_excluded = 1 WHERE item_id = :item_id",
            {"item_id": int(item_id)},
        )
    await mass.music.database.commit()

    whole = await controller.library_items(limit=0, **options)
    pages: list[Genre] = []
    for offset in range(len(whole)):
        page = await controller.library_items(limit=1, offset=offset, **options)
        assert len(page) == 1
        pages.extend(page)

    expected_ids = [item.item_id for item in before if item.item_id not in excluded]
    assert [item.item_id for item in whole] == expected_ids
    assert [item.item_id for item in pages] == expected_ids
