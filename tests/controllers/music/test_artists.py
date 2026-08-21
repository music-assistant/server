"""Tests for the artists controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.media_items import (
    Artist,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

from .helpers import create_track

if TYPE_CHECKING:
    import pytest

    from music_assistant.mass import MusicAssistant

ARTIST_MBID = "aa1b2c3d-1c99-4c1b-b0f1-9f2c1b2a3d44"
ARTIST_IMAGE = "http://images/artist1.jpg"


def _artist_stub() -> Artist:
    """Return the bare artist providers embed in their track payloads."""
    return Artist(
        item_id="track1_artist",
        provider="spotify_1",
        name="Test Artist",
        provider_mappings={
            ProviderMapping(
                item_id="track1_artist",
                provider_domain="spotify",
                provider_instance="spotify_1",
            )
        },
    )


def _detailed_artist() -> Artist:
    """Return an artist carrying the details only a full provider fetch delivers."""
    artist = _artist_stub()
    artist.external_ids = {(ExternalID.MB_ARTIST, ARTIST_MBID)}
    artist.metadata.description = "A biography"
    artist.metadata.genres = {"rock"}
    artist.metadata.images = UniqueList(
        [MediaItemImage(type=ImageType.THUMB, path=ARTIST_IMAGE, provider="spotify_1")]
    )
    return artist


async def test_overwrite_update_warns_when_no_provider_mappings_are_given(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An overwrite update carrying no provider mappings must emit a warning."""
    db_artist = await mass.music.artists.add_item_to_library(_artist_stub())

    update = _artist_stub()
    update.provider_mappings = set()
    await mass.music.artists.update_item_in_library(db_artist.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(db_artist.item_id)
    assert refreshed.provider_mappings == db_artist.provider_mappings
    assert (
        f"Ignoring request to clear all provider mappings of artist item id {db_artist.item_id}"
        in caplog.text
    )


async def test_track_overwrite_keeps_artist_details(mass: MusicAssistant) -> None:
    """A track update carrying a bare artist stub must not blank that artist's details."""
    db_artist = await mass.music.artists.add_item_to_library(_detailed_artist())
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList([_artist_stub()])
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(db_artist.item_id)
    assert refreshed.external_ids == {(ExternalID.MB_ARTIST, ARTIST_MBID)}
    assert refreshed.metadata.description == "A biography"
    assert refreshed.metadata.genres == {"rock"}
    assert [image.path for image in refreshed.metadata.images or []] == [ARTIST_IMAGE]


async def test_track_overwrite_keeps_other_provider_artist_mapping(mass: MusicAssistant) -> None:
    """A track update must not unlink its artist from the other providers it matched."""
    db_artist = await mass.music.artists.add_item_to_library(_artist_stub())
    await mass.music.artists.add_provider_mappings(
        db_artist.item_id,
        [
            ProviderMapping(
                item_id="tidal_artist1",
                provider_domain="tidal",
                provider_instance="tidal_1",
            )
        ],
    )
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList([_artist_stub()])
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(db_artist.item_id)
    assert {mapping.provider_instance for mapping in refreshed.provider_mappings} == {
        "spotify_1",
        "tidal_1",
    }


async def test_overwrite_update_replaces_artist_details(mass: MusicAssistant) -> None:
    """An overwrite update carrying details still replaces the stored ones."""
    db_artist = await mass.music.artists.add_item_to_library(_detailed_artist())

    update = _detailed_artist()
    update.external_ids = {(ExternalID.MB_ARTIST, "11111111-2222-3333-4444-555555555555")}
    update.metadata.description = "A better biography"
    update.metadata.images = UniqueList(
        [MediaItemImage(type=ImageType.THUMB, path="http://images/new.jpg", provider="spotify_1")]
    )
    await mass.music.artists.update_item_in_library(db_artist.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(db_artist.item_id)
    assert refreshed.external_ids == {
        (ExternalID.MB_ARTIST, "11111111-2222-3333-4444-555555555555")
    }
    assert refreshed.metadata.description == "A better biography"
    assert [image.path for image in refreshed.metadata.images or []] == ["http://images/new.jpg"]
