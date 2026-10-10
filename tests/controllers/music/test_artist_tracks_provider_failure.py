"""Tests for an artist's provider tracks when album tracklists on that provider fail."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import (
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

_PROVIDER = "streaming_inst"
_DOMAIN = "streaming"


def _album(name: str, provider: str = _PROVIDER) -> Album:
    return Album(item_id=name, provider=provider, name=name, provider_mappings=set())


def _track(name: str, *, available: bool = True) -> Track:
    mapping = ProviderMapping(
        item_id=name, provider_domain="streaming", provider_instance=_PROVIDER, available=available
    )
    return Track(item_id=name, provider=_PROVIDER, name=name, provider_mappings={mapping})


def _provider_without_artist_tracks() -> MagicMock:
    """Return a provider that has to fall back to enumerating the artist's albums."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = _PROVIDER
    provider.domain = _DOMAIN
    provider.available = True
    provider.supports_feature = MagicMock(
        side_effect=lambda feature: feature != ProviderFeature.ARTIST_TRACKS
    )
    return provider


def _listing(
    album_tracks: Callable[[str, str], Awaitable[list[Track]]],
) -> Callable[[str, str], Awaitable[tuple[list[Track], dict[str, Exception]]]]:
    """Return a fake album listing that reports no provider failures beside its tracks."""

    async def _tracks_with_lookup_errors(
        item_id: str, provider: str
    ) -> tuple[list[Track], dict[str, Exception]]:
        return await album_tracks(item_id, provider), {}

    return _tracks_with_lookup_errors


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
    # the provider is loaded and available; only one of its album tracklists errors
    await set_global_cache_values({"available_providers": {_PROVIDER}})
    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            return_value=[_album("Broken"), _album("Fine")],
        ),
        patch.object(
            mass.music.albums,
            "tracks_with_lookup_errors",
            side_effect=_listing(_failing_album_tracks({"Broken"})),
        ),
    ):
        tracks = await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)
    assert [track.name for track in tracks] == ["Fine track"]
    assert "Unable to fetch tracks for album Broken from provider streaming_inst" in caplog.text


def _album_tracks_failing_on(
    failing: str, error: Exception, requested: list[str]
) -> Callable[[str, str], Awaitable[list[Track]]]:
    """Return a fake album tracklist fetch that records each album and fails on one."""

    async def _tracks(item_id: str, _provider: str) -> list[Track]:
        requested.append(item_id)
        if item_id == failing:
            raise error
        return [_track(f"{item_id} track")]

    return _tracks


def _exhausted(cause: Exception) -> RetriesExhausted:
    """Return the error a provider raises once it gave up retrying on the given failure."""
    try:
        raise RetriesExhausted("Retries exhausted, failed after 8 attempts") from cause
    except RetriesExhausted as err:
        return err


async def test_provider_artist_tracks_stop_when_provider_is_rate_limited(
    mass: MusicAssistant,
) -> None:
    """A provider that gave up on a rate limit is not asked for the remaining albums."""
    await set_global_cache_values({"available_providers": {_PROVIDER}})
    requested: list[str] = []
    error = _exhausted(RateLimited("Apple Music Rate Limiter"))

    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            return_value=[_album("Fine"), _album("Limited"), _album("Later")],
        ),
        patch.object(
            mass.music.albums,
            "tracks_with_lookup_errors",
            side_effect=_listing(_album_tracks_failing_on("Limited", error, requested)),
        ),
    ):
        tracks = await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)
    assert requested == ["Fine", "Limited"]
    assert [track.name for track in tracks] == ["Fine track"]


async def test_provider_artist_tracks_go_on_after_one_album_keeps_failing(
    mass: MusicAssistant,
) -> None:
    """Retries running out on one album's own error do not cost the other albums."""
    await set_global_cache_values({"available_providers": {_PROVIDER}})
    requested: list[str] = []
    error = _exhausted(ResourceTemporarilyUnavailable("Apple Music API Timeout"))

    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            return_value=[_album("Fine"), _album("Broken"), _album("Later")],
        ),
        patch.object(
            mass.music.albums,
            "tracks_with_lookup_errors",
            side_effect=_listing(_album_tracks_failing_on("Broken", error, requested)),
        ),
    ):
        tracks = await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)
    assert requested == ["Fine", "Broken", "Later"]
    assert [track.name for track in tracks] == ["Fine track", "Later track"]


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
            "tracks_with_lookup_errors",
            side_effect=_listing(_failing_album_tracks({"Broken", "Also Broken"})),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)


async def test_provider_artist_tracks_raise_when_remaining_tracks_unavailable(
    mass: MusicAssistant,
) -> None:
    """Tracks a provider lists as unavailable do not count as playable: the error still surfaces."""
    # the provider itself is available: it is the tracks it lists that are not
    await set_global_cache_values({"available_providers": {_PROVIDER}})

    async def _album_tracks(item_id: str, _provider: str) -> list[Track]:
        if item_id == "Broken":
            raise MediaNotFoundError("Failed to get album tracks for Broken")
        return [_track("Trashed track", available=False)]

    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            return_value=[_album("Broken"), _album("Trashed")],
        ),
        patch.object(
            mass.music.albums, "tracks_with_lookup_errors", side_effect=_listing(_album_tracks)
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)


async def test_provider_artist_tracks_stop_when_library_album_is_rate_limited(
    mass: MusicAssistant,
) -> None:
    """A library album keeps its library tracks, and its provider's rate limit still stops."""
    await set_global_cache_values({"available_providers": {_PROVIDER}})
    requested: list[str] = []
    error = _exhausted(RateLimited("Apple Music Rate Limiter"))

    async def _album_listing(
        item_id: str, _provider: str
    ) -> tuple[list[Track], dict[str, Exception]]:
        requested.append(item_id)
        if item_id == "InLibrary":
            return [_track("InLibrary track")], {_PROVIDER: error}
        return [_track(f"{item_id} track")], {}

    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            return_value=[_album("Fine"), _album("InLibrary"), _album("Later")],
        ),
        patch.object(mass.music.albums, "tracks_with_lookup_errors", side_effect=_album_listing),
    ):
        tracks = await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)
    assert requested == ["Fine", "InLibrary"]
    assert [track.name for track in tracks] == ["Fine track", "InLibrary track"]


async def test_provider_artist_tracks_stop_when_library_album_of_domain_album_is_rate_limited(
    mass: MusicAssistant,
) -> None:
    """A provider album named by domain still finds its rate limit, reported by instance id."""
    await set_global_cache_values({"available_providers": {_PROVIDER}})
    requested: list[str] = []
    error = _exhausted(RateLimited("Apple Music Rate Limiter"))

    async def _album_listing(
        item_id: str, _provider: str
    ) -> tuple[list[Track], dict[str, Exception]]:
        requested.append(item_id)
        if item_id == "InLibrary":
            # the library album names its failing provider mapping by instance id
            return [_track("InLibrary track")], {_PROVIDER: error}
        return [_track(f"{item_id} track")], {}

    albums = [_album(name, provider=_DOMAIN) for name in ("Fine", "InLibrary", "Later")]
    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(mass.music.artists, "get_provider_artist_albums", return_value=albums),
        patch.object(mass.music.albums, "tracks_with_lookup_errors", side_effect=_album_listing),
    ):
        tracks = await mass.music.artists.get_provider_artist_tracks("artist1", _DOMAIN)
    assert requested == ["Fine", "InLibrary"]
    assert [track.name for track in tracks] == ["Fine track", "InLibrary track"]
