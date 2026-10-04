"""Tests for the artwork a library listing shows when the item is on several music sources."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ImageType, ProviderSharing
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

GET_CURRENT_USER = "music_assistant.controllers.music.media.base.get_current_user"
# two accounts on the same Subsonic server, each owned by (and private to) one user
SONIC_A = "opensubsonic--a"
SONIC_B = "opensubsonic--b"
USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)
USER_B = User(user_id="user-b", username="user-b", role=UserRole.USER)


@pytest.fixture(scope="module")
async def seeded_mass(music_mass_module: MusicAssistant) -> MusicAssistant:
    """Return a library holding an artist, album and track found on both accounts."""
    mass = music_mass_module
    set_music_source_access(
        mass,
        {
            SONIC_A: ProviderAccess(owner=USER_A.user_id, sharing=ProviderSharing.PRIVATE),
            SONIC_B: ProviderAccess(owner=USER_B.user_id, sharing=ProviderSharing.PRIVATE),
        },
    )
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="ar-1",
            provider=SONIC_A,
            name="Shared Artist",
            provider_mappings=_mappings("ar-1"),
            metadata=_metadata_with_covers("ar-1"),
        )
    )
    album = await mass.music.albums.add_item_to_library(
        Album(
            item_id="al-1",
            provider=SONIC_A,
            name="Shared Album",
            provider_mappings=_mappings("al-1"),
            artists=UniqueList([artist]),
            metadata=_metadata_with_covers("al-1"),
        )
    )
    await mass.music.tracks.add_item_to_library(
        Track(
            item_id="tr-1",
            provider=SONIC_A,
            name="Shared Track",
            provider_mappings=_mappings("tr-1"),
            artists=UniqueList([artist]),
            album=album,
            disc_number=1,
            track_number=1,
        )
    )
    return mass


def _mappings(item_id: str) -> set[ProviderMapping]:
    """Return the in-library mappings of an item on both accounts."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="opensubsonic",
            provider_instance=instance_id,
            in_library=True,
        )
        for instance_id in (SONIC_A, SONIC_B)
    }


def _metadata_with_covers(cover_art_id: str) -> MediaItemMetadata:
    """Return metadata carrying the (same) cover of both accounts, the one of B first."""
    return MediaItemMetadata(
        images=UniqueList(
            [
                MediaItemImage(type=ImageType.THUMB, path=cover_art_id, provider=instance_id)
                for instance_id in (SONIC_B, SONIC_A)
            ]
        )
    )


@pytest.fixture
def as_user(request: pytest.FixtureRequest) -> Iterator[User | None]:
    """Run the test as the (parametrized) current user."""
    with patch(GET_CURRENT_USER, return_value=request.param):
        yield request.param


def _own_source(user: User | None) -> str:
    """Return the account whose cover the user is shown; the first stored one without a user."""
    return SONIC_A if user is USER_A else SONIC_B


@pytest.mark.parametrize("as_user", [USER_A, USER_B, None], indirect=True)
async def test_artist_listing_shows_the_cover_of_the_users_own_source(
    seeded_mass: MusicAssistant, as_user: User | None
) -> None:
    """A user is shown the artist cover of their own account, not the one hidden from them."""
    (artist,) = await seeded_mass.music.artists.library_items()

    assert artist.image is not None
    assert artist.image.provider == _own_source(as_user)


@pytest.mark.parametrize("as_user", [USER_A, USER_B, None], indirect=True)
async def test_track_listing_shows_the_album_cover_of_the_users_own_source(
    seeded_mass: MusicAssistant, as_user: User | None
) -> None:
    """A track (summary) row shows the album cover of the user's own account."""
    (track,) = await seeded_mass.music.tracks.library_items()

    assert track.album is not None
    assert track.album.image is not None
    assert track.album.image.provider == _own_source(as_user)
    assert track.image is not None
    assert track.image.provider == _own_source(as_user)


@pytest.mark.parametrize("as_user", [USER_A, USER_B, None], indirect=True)
async def test_full_track_shows_the_album_cover_of_the_users_own_source(
    seeded_mass: MusicAssistant, as_user: User | None
) -> None:
    """A full track carries the album cover of the user's own account as its album image."""
    (track,) = await seeded_mass.music.tracks.library_items(summary=False)

    assert isinstance(track, Track)
    assert track.album is not None
    assert track.album.image is not None
    assert track.album.image.provider == _own_source(as_user)
    assert track.image is not None
    assert track.image.provider == _own_source(as_user)
