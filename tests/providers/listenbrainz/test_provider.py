"""Tests for ListenBrainz playlist artist resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.parsers import JSPF_TRACK_EXTENSION

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

LIBRARY_ARTIST_MBID = "11111111-1111-4111-8111-111111111111"
UNMATCHED_ARTIST_MBID = "22222222-2222-4222-8222-222222222222"
LIBRARY_RECORDING_MBID = "33333333-3333-4333-8333-333333333333"
UNMATCHED_RECORDING_MBID = "44444444-4444-4444-8444-444444444444"


def _provider(library_artist: Artist, library_track: Track | None = None) -> ListenBrainzProvider:
    """Create a provider with optional items already present in the MA library."""
    provider = ListenBrainzProvider.__new__(ListenBrainzProvider)
    provider.manifest = cast("ProviderManifest", MagicMock(domain="listenbrainz"))
    provider.config = cast(
        "ProviderConfig",
        MagicMock(instance_id="listenbrainz--test", name=None),
    )
    mass = MagicMock()
    mass.music.tracks.get_library_item_by_external_id = AsyncMock(
        side_effect=lambda mbid, external_id_type: (
            library_track
            if mbid == LIBRARY_RECORDING_MBID and external_id_type == ExternalID.MB_RECORDING
            else None
        )
    )
    mass.music.artists.get_library_item_by_external_id = AsyncMock(
        side_effect=lambda mbid, external_id_type: (
            library_artist
            if mbid == LIBRARY_ARTIST_MBID and external_id_type == ExternalID.MB_ARTIST
            else None
        )
    )
    provider.mass = cast("MusicAssistant", mass)
    return provider


def _track_data(
    recording_mbid: str, artists: list[tuple[str, str]], title: str = "Test Track"
) -> dict[str, object]:
    """Return a minimal raw ListenBrainz JSPF track."""
    return {
        "album": "Test Album",
        "duration": 180000,
        "extension": {
            JSPF_TRACK_EXTENSION: {
                "additional_metadata": {
                    "artists": [
                        {
                            "artist_credit_name": name,
                            "artist_mbid": mbid,
                            "join_phrase": "",
                        }
                        for name, mbid in artists
                    ]
                }
            }
        },
        "identifier": [f"https://musicbrainz.org/recording/{recording_mbid}"],
        "title": title,
    }


@pytest.mark.asyncio
async def test_playlist_tracks_link_artists_by_musicbrainz_id() -> None:
    """Known artist MBIDs link to library artists and unknown ones retain their MBIDs."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    provider = _provider(library_artist)
    track = await provider._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [
                ("Library Artist", LIBRARY_ARTIST_MBID),
                ("Unmatched Artist", UNMATCHED_ARTIST_MBID),
            ],
        ),
        1,
    )

    assert track.artists[0] is library_artist
    assert [artist.name for artist in track.artists] == [
        "Library Artist",
        "Unmatched Artist",
    ]
    assert track.artists[1].get_external_id(ExternalID.MB_ARTIST) == UNMATCHED_ARTIST_MBID


@pytest.mark.asyncio
async def test_playlist_tracks_reuse_library_track_by_recording_mbid() -> None:
    """An exact recording MBID match returns the existing playable library track."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    library_track = Track(
        item_id="7",
        provider="library",
        name="Library Track",
        duration=180,
        artists=UniqueList([library_artist]),
        provider_mappings={
            ProviderMapping(
                item_id="provider-track-id",
                provider_domain="test_music",
                provider_instance="test_music--instance",
            )
        },
        external_ids={(ExternalID.MB_RECORDING, LIBRARY_RECORDING_MBID)},
    )
    provider = _provider(library_artist, library_track)

    track = await provider._parse_track(
        _track_data(
            LIBRARY_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
            title="Library Track",
        ),
        1,
    )

    assert track is library_track
    assert track.position == 1
