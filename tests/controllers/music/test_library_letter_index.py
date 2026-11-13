"""Tests for the letter index of the artist, album and track library listings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import (
    AlbumType,
    ArtistType,
    MediaType,
    SortDirection,
    SortField,
)
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

if TYPE_CHECKING:
    from music_assistant.controllers.music.media.albums import AlbumsController
    from music_assistant.controllers.music.media.artists import ArtistsController
    from music_assistant.controllers.music.media.tracks import TracksController
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

GET_CURRENT_USER = "music_assistant.controllers.music.media.base.get_current_user"
LISTING_USER = User(user_id="letter-user", username="letter-user", role=UserRole.USER)
# "The Cure" starts with T by name but with C by sort name, "!!!" has no name characters at all
NAMES = ("The Cure", "Abba", "ABC", "Beatles", "10cc", "!!!", "Zappa", "Björk", "Cake")
NAME_SORTS = [
    (field, direction)
    for field in (SortField.SORT_NAME, SortField.NAME)
    for direction in (SortDirection.ASC, SortDirection.DESC)
]


@pytest.fixture(scope="module")
async def seeded_mass(music_mass_module: MusicAssistant) -> MusicAssistant:
    """Return a module-scoped database-only instance with a seeded library."""
    mass = music_mass_module
    artists: list[Artist] = []
    for idx, name in enumerate(NAMES):
        artist = await mass.music.artists.add_item_to_library(
            Artist(item_id="0", provider="library", name=name, provider_mappings={_mapping()})
        )
        artists.append(artist)
        album = await mass.music.albums.add_item_to_library(
            Album(
                item_id="0",
                provider="library",
                name=name,
                album_type=AlbumType.SINGLE if idx % 2 else AlbumType.ALBUM,
                provider_mappings={_mapping()},
                artists=UniqueList([artist]),
            )
        )
        track = await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name=name,
                provider_mappings={_mapping()},
                artists=UniqueList([artist]),
            )
        )
        if idx % 3 == 0:
            for media_type, item_id in (
                (MediaType.ARTIST, artist.item_id),
                (MediaType.ALBUM, album.item_id),
                (MediaType.TRACK, track.item_id),
            ):
                await mass.music.favorites.set(
                    media_type, int(item_id), True, [LISTING_USER.user_id]
                )
    await mass.music.database.commit()
    return mass


@pytest.mark.parametrize("api_base", ["artists", "albums", "tracks"])
async def test_letter_index_command_registered(seeded_mass: MusicAssistant, api_base: str) -> None:
    """The letter index is served for the artist, album and track libraries."""
    assert f"music/{api_base}/letter_index" in seeded_mass.command_handlers


async def test_artist_letter_index_by_sort_name(seeded_mass: MusicAssistant) -> None:
    """Each letter points at its first artist, with "#" for names without a letter."""
    with patch(GET_CURRENT_USER, return_value=LISTING_USER):
        index = await seeded_mass.music.artists.letter_index(sort_field=SortField.SORT_NAME)
    assert index == [
        {"letter": "#", "offset": 0},
        {"letter": "A", "offset": 2},
        {"letter": "B", "offset": 4},
        {"letter": "C", "offset": 6},
        {"letter": "Z", "offset": 8},
    ]


@pytest.mark.parametrize(("sort_field", "sort_direction"), NAME_SORTS)
@pytest.mark.parametrize("controller_name", ["artists", "albums", "tracks"])
async def test_letter_index_matches_listing(
    seeded_mass: MusicAssistant,
    controller_name: str,
    sort_field: SortField,
    sort_direction: SortDirection,
) -> None:
    """The offsets point at the first item of each letter in the listing for the same sort."""
    controller = getattr(seeded_mass.music, controller_name)
    await _assert_matches_listing(controller, sort_field=sort_field, sort_direction=sort_direction)


@pytest.mark.parametrize(("sort_field", "sort_direction"), NAME_SORTS)
@pytest.mark.parametrize("controller_name", ["artists", "albums", "tracks"])
async def test_letter_index_applies_favorite_filter(
    seeded_mass: MusicAssistant,
    controller_name: str,
    sort_field: SortField,
    sort_direction: SortDirection,
) -> None:
    """The offsets follow the listing narrowed to the user's likes."""
    controller = getattr(seeded_mass.music, controller_name)
    index = await _assert_matches_listing(
        controller, favorite=True, sort_field=sort_field, sort_direction=sort_direction
    )
    # the liked items are The Cure, Beatles and Zappa
    expected = {"B", "C", "Z"} if sort_field == SortField.SORT_NAME else {"B", "T", "Z"}
    assert {entry["letter"] for entry in index} == expected


@pytest.mark.parametrize("sort_direction", [SortDirection.ASC, SortDirection.DESC])
async def test_album_letter_index_applies_album_types(
    seeded_mass: MusicAssistant, sort_direction: SortDirection
) -> None:
    """The offsets follow the album listing narrowed to the requested album types."""
    await _assert_matches_listing(
        seeded_mass.music.albums,
        album_types=[AlbumType.SINGLE],
        sort_field=SortField.SORT_NAME,
        sort_direction=sort_direction,
    )


@pytest.mark.parametrize("sort_direction", [SortDirection.ASC, SortDirection.DESC])
async def test_artist_letter_index_applies_artist_filters(
    seeded_mass: MusicAssistant, sort_direction: SortDirection
) -> None:
    """The offsets follow the artist listing narrowed to album artists of the singer type."""
    index = await _assert_matches_listing(
        seeded_mass.music.artists,
        album_artists_only=True,
        artist_type=ArtistType.SINGER,
        sort_field=SortField.SORT_NAME,
        sort_direction=sort_direction,
    )
    assert index


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sort_field": SortField.TIMESTAMP_ADDED},
        {"sort_field": SortField.RANDOM},
        {"sort_field": SortField.SORT_NAME, "search": "abba"},
        {"sort_field": SortField.SORT_NAME, "reachable_via": []},
    ],
)
@pytest.mark.parametrize("controller_name", ["artists", "albums", "tracks"])
async def test_letter_index_empty(
    seeded_mass: MusicAssistant, controller_name: str, kwargs: dict[str, Any]
) -> None:
    """A listing not sorted by name, a search or a listing without items has no letter index."""
    controller = getattr(seeded_mass.music, controller_name)
    with patch(GET_CURRENT_USER, return_value=LISTING_USER):
        assert await controller.letter_index(**kwargs) == []


async def _assert_matches_listing(
    controller: ArtistsController | AlbumsController | TracksController, **kwargs: Any
) -> list[dict[str, str | int]]:
    """Assert the letter index matches the first item of each letter in the full listing."""
    with patch(GET_CURRENT_USER, return_value=LISTING_USER):
        index = await controller.letter_index(**kwargs)
        items = await controller.library_items(limit=0, **kwargs)
    by_sort_name = kwargs["sort_field"] == SortField.SORT_NAME
    letters = [_letter(item.sort_name if by_sort_name else item.name) for item in items]
    expected = [
        {"letter": letter, "offset": idx}
        for idx, letter in enumerate(letters)
        if idx == 0 or letters[idx - 1] != letter
    ]
    assert index == expected
    # every letter is one run of items, so a jump to it reaches all of them
    assert len({entry["letter"] for entry in index}) == len(index)
    return index


def _letter(name: str | None) -> str:
    """Return the letter an item with this name is listed under."""
    first = create_safe_string(name or "", True, True)[:1]
    return first.upper() if "a" <= first <= "z" else "#"


def _mapping() -> ProviderMapping:
    """Create an in-library provider mapping with a unique provider item id."""
    return ProviderMapping(
        item_id=uuid4().hex,
        provider_domain="prov_a",
        provider_instance="prov_a_inst",
        in_library=True,
    )
