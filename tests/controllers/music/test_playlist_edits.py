"""Tests for reading a playlist on a music service after Music Assistant changed it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import Playlist, ProviderMapping, Track

from music_assistant.controllers.cache.helpers import use_cache
from music_assistant.controllers.tasks.context import report_current_task_failure
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INSTANCE = "spotify_1"
PLAYLIST_ID = "pl1"


class _EditablePlaylistProvider(MusicProvider):
    """Music service holding one editable playlist, keeping its pages as the services do."""

    def __init__(self, mass: MusicAssistant, track_ids: list[str]) -> None:
        """
        Initialize the stub provider.

        :param mass: The MusicAssistant instance, whose cache keeps the pages.
        :param track_ids: The ids of the tracks of the playlist, in playlist order.
        """
        self.mass = mass
        self.track_ids = list(track_ids)
        # the ids the service refuses to add, each reported as a failure of the task
        self.refused: set[str] = set()
        self.pages_fetched = 0
        self._supported_features = {ProviderFeature.PLAYLIST_TRACKS_EDIT}
        self.config = MagicMock()
        self.config.instance_id = INSTANCE
        self.manifest = MagicMock()
        self.manifest.domain = INSTANCE.split("_", maxsplit=1)[0]
        self.logger = MagicMock()
        self.available = True

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return one page of two tracks of the playlist."""
        self.pages_fetched += 1
        tracks = []
        start = page * 2
        for position, track_id in enumerate(self.track_ids[start : start + 2], start=start + 1):
            track = create_track(INSTANCE, track_id)
            track.position = position
            tracks.append(track)
        return tracks

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Append the tracks to the playlist, reporting the refused ones as failures."""
        for track_id in prov_track_ids:
            if track_id in self.refused:
                report_current_task_failure(f"Can't add {track_id}")
            else:
                self.track_ids.append(track_id)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove the tracks at the given positions, 1-based."""
        self.track_ids = [
            track_id
            for position, track_id in enumerate(self.track_ids, start=1)
            if position not in positions_to_remove
        ]


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_audio_analysis: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store and the stored audio analysis."""
    return music_mass_with_audio_analysis


def _with_provider(mass: MusicAssistant, provider: MusicProvider) -> Any:
    """Load the given provider as the only one, under its instance id and its domain."""

    def _get_provider(provider_id: str, *_args: Any, **_kwargs: Any) -> Any:
        return provider if provider_id in (provider.instance_id, provider.domain) else None

    return patch.object(mass, "get_provider", side_effect=_get_provider)


async def _library_playlist(mass: MusicAssistant) -> Playlist:
    """Add the playlist of the service to the library and return the library item."""
    playlist = Playlist(
        item_id=PLAYLIST_ID,
        provider=INSTANCE,
        name="Edited",
        provider_mappings={
            ProviderMapping(
                item_id=PLAYLIST_ID,
                provider_domain=INSTANCE.split("_", maxsplit=1)[0],
                provider_instance=INSTANCE,
                in_library=True,
            )
        },
        is_editable=True,
    )
    return await mass.music.playlists.add_item_to_library(playlist)


async def _add(mass: MusicAssistant, playlist: Playlist, *track_ids: str) -> None:
    """Add the tracks to the playlist as an internal caller, as the queued task does."""
    uris = [f"{INSTANCE}://track/{track_id}" for track_id in track_ids]
    await mass.music.playlists._handle_add_playlist_tracks(playlist.item_id, uris, None)


async def _listed(mass: MusicAssistant, playlist: Playlist) -> list[str]:
    """Return the ids the playlist page lists, as the frontend asks for them."""
    items = await mass.music.playlists.tracks(playlist.item_id, "library", limit=0)
    return [item.item_id for item in items]


async def test_a_track_added_twice_is_in_the_playlist_once(mass: MusicAssistant) -> None:
    """The second add of a track sees the first one and adds nothing."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        await _add(mass, playlist, "t3")
        await _add(mass, playlist, "t3")
    assert provider.track_ids == ["t1", "t2", "t3"]


async def test_a_listing_after_an_add_shows_the_added_track(mass: MusicAssistant) -> None:
    """A playlist listed before and after an add lists the added track."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        assert await _listed(mass, playlist) == ["t1", "t2"]
        await _add(mass, playlist, "t3")
        assert await _listed(mass, playlist) == ["t1", "t2", "t3"]


async def test_a_removed_track_can_be_added_again(mass: MusicAssistant) -> None:
    """A track removed from the playlist is listed no more and is not skipped when added again."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2", "t3"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        assert await _listed(mass, playlist) == ["t1", "t2", "t3"]
        await mass.music.playlists._handle_remove_playlist_tracks(playlist.item_id, (2,), None)
        assert await _listed(mass, playlist) == ["t1", "t3"]
        await _add(mass, playlist, "t2")
    assert provider.track_ids == ["t1", "t3", "t2"]


async def test_a_partly_successful_add_run_again_adds_nothing_twice(mass: MusicAssistant) -> None:
    """Running an add again after the service refused part of it adds only what was missing."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2"])
    provider.refused = {"t4"}
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        await _add(mass, playlist, "t3", "t4")
        assert provider.track_ids == ["t1", "t2", "t3"]
        provider.refused = set()
        # the retry of the task runs its handler again with the same items
        await _add(mass, playlist, "t3", "t4")
    assert provider.track_ids == ["t1", "t2", "t3", "t4"]


async def test_a_change_fetches_nothing_from_the_service(mass: MusicAssistant) -> None:
    """The playlist is fetched anew by the next reader, not when it changes."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2", "t3"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        await _listed(mass, playlist)
        provider.pages_fetched = 0
        # the add reads the kept playlist to leave out what it holds already
        await _add(mass, playlist, "t4")
        await mass.music.playlists._handle_remove_playlist_tracks(playlist.item_id, (1,), None)
        assert provider.pages_fetched == 0
        assert await _listed(mass, playlist) == ["t2", "t3", "t4"]
    # two pages and the empty one that ends the playlist
    assert provider.pages_fetched == 3
