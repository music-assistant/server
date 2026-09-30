"""Tests for the artists controller."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, ImageType, ProviderFeature
from music_assistant_models.media_items import (
    Artist,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

from music_assistant.controllers.music.media.artists import ArtistsController

from .helpers import create_track

if TYPE_CHECKING:
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


@pytest.mark.parametrize(
    ("feature", "library_method", "provider_method"),
    [
        (
            ProviderFeature.ARTIST_TOPTRACKS,
            "get_library_artist_toptracks",
            "get_provider_artist_toptracks",
        ),
        (
            ProviderFeature.ARTIST_TOPALBUMS,
            "get_library_artist_topalbums",
            "get_provider_artist_topalbums",
        ),
        (
            ProviderFeature.SIMILAR_ARTISTS,
            "get_library_artist_similar_artists",
            "get_provider_artist_similar_artists",
        ),
    ],
)
async def test_library_artist_listings_query_one_instance_per_streaming_domain(
    feature: ProviderFeature, library_method: str, provider_method: str
) -> None:
    """Instances of one streaming provider return the same catalog, so only one is queried."""
    artist = Artist(
        item_id="1",
        provider="library",
        name="Test Artist",
        provider_mappings={
            ProviderMapping(
                item_id=f"artist_{instance}", provider_domain=domain, provider_instance=instance
            )
            for domain, instance in (
                ("spotify", "spotify_1"),
                ("spotify", "spotify_2"),
                ("filesystem_local", "filesystem_local_1"),
                ("filesystem_local", "filesystem_local_2"),
            )
        },
    )
    providers = {
        instance: SimpleNamespace(
            domain=instance.rsplit("_", maxsplit=1)[0],
            is_streaming_provider=instance.startswith("spotify"),
            supported_features={feature},
        )
        for instance in ("spotify_1", "spotify_2", "filesystem_local_1", "filesystem_local_2")
    }
    mass = MagicMock()
    mass.get_provider = MagicMock(side_effect=lambda instance, **_kwargs: providers[instance])
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    ctrl = ArtistsController.__new__(ArtistsController)
    ctrl.mass = mass
    ctrl.logger = logging.getLogger("test.artists.listings")
    provider_listing = AsyncMock(return_value=[])
    with patch.multiple(
        ctrl,
        get_library_item=AsyncMock(return_value=artist),
        **{provider_method: provider_listing},
    ):
        await getattr(ctrl, library_method)("1")

    queried = {call.args[1] for call in provider_listing.await_args_list}
    # one Spotify instance, and both (non-streaming) filesystem instances
    assert queried == {"spotify_1", "filesystem_local_1", "filesystem_local_2"}
