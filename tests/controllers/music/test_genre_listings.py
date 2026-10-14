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
