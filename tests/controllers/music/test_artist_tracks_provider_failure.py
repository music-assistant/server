"""Tests for an artist's provider tracks when album tracklists on that provider fail."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

_PROVIDER = "streaming_inst"


def _album(name: str) -> Album:
    return Album(item_id=name, provider=_PROVIDER, name=name, provider_mappings=set())


def _track(name: str) -> Track:
    mapping = ProviderMapping(
        item_id=name, provider_domain="streaming", provider_instance=_PROVIDER
    )
    return Track(item_id=name, provider=_PROVIDER, name=name, provider_mappings={mapping})


def _provider_without_artist_tracks() -> MagicMock:
    """Return a provider that has to fall back to enumerating the artist's albums."""
    provider = MagicMock(spec=MusicProvider)
    provider.available = True
    provider.supports_feature = MagicMock(
        side_effect=lambda feature: feature != ProviderFeature.ARTIST_TRACKS
    )
    return provider


def _failing_album_tracks(
    failing: set[str],
) -> Callable[[str, str], Awaitable[list[Track]]]:
    """Return a fake album tracklist fetch that fails for the given album ids only."""

    async def _tracks(item_id: str, _provider: str) -> list[Track]:
        if item_id in failing:
            raise MediaNotFoundError(f"Failed to get album tracks for {item_id}")
        return [_track(f"{item_id} track")]

    return _tracks


async def test_provider_artist_tracks_skip_failing_album(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """One album whose tracks cannot be fetched is skipped (and logged); the others are kept."""
    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            return_value=[_album("Broken"), _album("Fine")],
        ),
        patch.object(mass.music.albums, "tracks", side_effect=_failing_album_tracks({"Broken"})),
    ):
        tracks = await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)
    assert [track.name for track in tracks] == ["Fine track"]
    assert "Unable to fetch tracks for album Broken from provider streaming_inst" in caplog.text


async def test_provider_artist_tracks_raise_when_every_album_fails(mass: MusicAssistant) -> None:
    """With every album failing and nothing to list, the provider error still surfaces."""
    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            return_value=[_album("Broken"), _album("Also Broken")],
        ),
        patch.object(
            mass.music.albums,
            "tracks",
            side_effect=_failing_album_tracks({"Broken", "Also Broken"}),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)
