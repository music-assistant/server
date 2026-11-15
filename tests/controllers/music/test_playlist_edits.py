"""Tests for reading a playlist on a music service after Music Assistant changed it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import (
    EventType,
    ListingType,
    MediaType,
    ProviderFeature,
    TaskStatus,
)
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import Playlist, ProviderMapping, Track

from music_assistant.constants import DB_TABLE_CACHE
from music_assistant.controllers.cache.helpers import forget_cached, use_cache
from music_assistant.controllers.music.constants import CACHE_CATEGORY_LISTINGS
from music_assistant.controllers.tasks.context import report_current_task_failure
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_track, media_item_events

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
        # an add stops after its first item: it raises this error, or waits for resume_add
        self.add_error: Exception | None = None
        self.resume_add = asyncio.Event()
        self.resume_add.set()
        # a page is read when it is asked for, and handed back once the gate is open
        self.gate = asyncio.Event()
        self.gate.set()
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
        tracks = self._page(page)
        await self.gate.wait()
        return tracks

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Append the tracks to the playlist, reporting the refused ones as failures."""
        for track_id in prov_track_ids:
            if track_id in self.refused:
                report_current_task_failure(f"Can't add {track_id}")
                continue
            self.track_ids.append(track_id)
            if self.add_error is not None:
                raise self.add_error
            await self.resume_add.wait()

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove the tracks at the given positions, 1-based."""
        self.track_ids = [
            track_id
            for position, track_id in enumerate(self.track_ids, start=1)
            if position not in positions_to_remove
        ]

    def _page(self, page: int) -> list[Track]:
        """Return one page of two tracks of the playlist as it is now."""
        tracks = []
        start = page * 2
        for position, track_id in enumerate(self.track_ids[start : start + 2], start=start + 1):
            track = create_track(INSTANCE, track_id)
            track.position = position
            tracks.append(track)
        return tracks


class _MarkedPlaylistProvider(_EditablePlaylistProvider):
    """Music service keeping its pages under a change marker of the playlist, as Apple Music."""

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return one page of the playlist, kept under the playlist's change marker."""
        marker = await self._change_marker(prov_playlist_id)
        return await self._marked_page(prov_playlist_id, page, marker)

    async def forget_playlist_tracks(self, prov_playlist_id: str) -> None:
        """Drop the kept pages of the playlist and its change marker."""
        await forget_cached(self._change_marker, prov_playlist_id)
        await forget_cached(self._marked_page, prov_playlist_id)

    @use_cache(15 * 60, allow_bypass=False)
    async def _change_marker(self, prov_playlist_id: str) -> str:
        """Return a marker that moves with every change of the playlist."""
        return str(len(self.track_ids))

    @use_cache(3600 * 24 * 7)
    async def _marked_page(self, prov_playlist_id: str, page: int, marker: str) -> list[Track]:
        """Return one page of the playlist."""
        self.pages_fetched += 1
        return self._page(page)


class _PositionLookupProvider(_EditablePlaylistProvider):
    """Music service whose removal looks the positions up in the playlist, as NicoVideo."""

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove the tracks the playlist lists at the given positions, 1-based."""
        listed = await self.get_playlist_tracks(prov_playlist_id)
        removed = {track.item_id for track in listed if track.position in positions_to_remove}
        self.track_ids = [track_id for track_id in self.track_ids if track_id not in removed]


class _ForgetFailingProvider(_EditablePlaylistProvider):
    """Music service whose kept pages can not be dropped."""

    async def forget_playlist_tracks(self, prov_playlist_id: str) -> None:
        """Fail to drop the kept pages."""
        raise RuntimeError("the cache is not available")


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


def _uris(*track_ids: str) -> list[str]:
    return [f"{INSTANCE}://track/{track_id}" for track_id in track_ids]


async def _add(mass: MusicAssistant, playlist: Playlist, *track_ids: str) -> None:
    """Add the tracks to the playlist as an internal caller, as the queued task does."""
    await mass.music.playlists._handle_add_playlist_tracks(
        playlist.item_id, _uris(*track_ids), None
    )


async def _remove(mass: MusicAssistant, playlist: Playlist, *positions: int) -> None:
    """Remove the tracks at the positions as an internal caller, as the queued task does."""
    await mass.music.playlists._handle_remove_playlist_tracks(playlist.item_id, positions, None)


async def _listed(mass: MusicAssistant, playlist: Playlist) -> list[str]:
    """Return the ids the playlist page lists, as the frontend asks for them."""
    items = await mass.music.playlists.tracks(playlist.item_id, "library", limit=0)
    return [item.item_id for item in items]


async def _wait_for(condition: Callable[[], bool]) -> None:
    """Wait until the condition holds."""
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition was never met")


async def _wait_for_kept(mass: MusicAssistant, count: int) -> None:
    """Wait until the provider keeps the given number of results in the cache."""
    assert mass.cache.database is not None
    query = f"SELECT key FROM {DB_TABLE_CACHE} WHERE provider = :provider"
    for _ in range(200):
        if await mass.cache.database.get_count_from_query(query, {"provider": INSTANCE}) == count:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"the provider never kept {count} results")


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
        await _remove(mass, playlist, 2)
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


async def test_a_partly_successful_add_retried_as_a_task_adds_nothing_twice(
    mass: MusicAssistant,
) -> None:
    """The retry of a partly successful add task adds only what the first run left out."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2"])
    provider.refused = {"t4"}
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        task = await mass.music.playlists.add_playlist_tracks(playlist.item_id, _uris("t3", "t4"))
        await _wait_for(lambda: mass.tasks.get_task(task.id).status == TaskStatus.PARTIAL_SUCCESS)
        provider.refused = set()
        mass.tasks.retry_task(task.id)
        await _wait_for(lambda: mass.tasks.get_task(task.id).status == TaskStatus.SUCCESS)
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
        await _remove(mass, playlist, 1)
        assert provider.pages_fetched == 0
        assert await _listed(mass, playlist) == ["t2", "t3", "t4"]
    # two pages and the empty one that ends the playlist
    assert provider.pages_fetched == 3


async def test_a_refresh_on_its_way_does_not_bring_back_the_playlist_before_an_add(
    mass: MusicAssistant,
) -> None:
    """A refresh the add started of a copy that ran out keeps nothing once the add is done."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        await _listed(mass, playlist)
        # the first page and the empty one that ends the playlist
        await _wait_for_kept(mass, 2)
        assert mass.cache.database is not None
        await mass.cache.database.execute_write(
            f"UPDATE {DB_TABLE_CACHE} SET expires = 0 WHERE provider = :provider",
            {"provider": INSTANCE},
        )
        # the add reads the pages that ran out and starts a refresh of each, which reads the
        # playlist as it is before the add and hands it back after the add is done
        provider.gate.clear()
        await _add(mass, playlist, "t3")
        refreshes = [
            task
            for task in mass._tracked_tasks.values()
            if task.get_name() == f"cache_refresh_{INSTANCE}"
        ]
        assert len(refreshes) == 2
        provider.gate.set()
        await asyncio.gather(*refreshes)
        assert await _listed(mass, playlist) == ["t1", "t2", "t3"]


async def test_pages_kept_under_a_change_marker_are_read_anew_after_an_add(
    mass: MusicAssistant,
) -> None:
    """A provider that keeps its pages its own way drops them through its own method."""
    provider = _MarkedPlaylistProvider(mass, ["t1", "t2"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        assert await _listed(mass, playlist) == ["t1", "t2"]
        await _add(mass, playlist, "t3")
        assert await _listed(mass, playlist) == ["t1", "t2", "t3"]


async def test_a_removal_reading_the_whole_playlist_sees_an_earlier_removal(
    mass: MusicAssistant,
) -> None:
    """A provider reading the playlist without a page sees it as an earlier removal left it."""
    provider = _PositionLookupProvider(mass, ["t1", "t2", "t3"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        await _remove(mass, playlist, 1)
        await _remove(mass, playlist, 1)
    assert provider.track_ids == ["t3"]


async def test_an_add_cancelled_part_way_is_listed_as_far_as_it_got(mass: MusicAssistant) -> None:
    """An add cancelled after its first item lists that item, and stays cancelled."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2"])
    provider.resume_add.clear()
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        assert await _listed(mass, playlist) == ["t1", "t2"]
        add = asyncio.create_task(_add(mass, playlist, "t3", "t4"))
        await _wait_for(lambda: "t3" in provider.track_ids)
        add.cancel()
        with pytest.raises(asyncio.CancelledError):
            await add
        assert await _listed(mass, playlist) == ["t1", "t2", "t3"]


async def test_an_add_failing_part_way_is_listed_as_far_as_it_got(mass: MusicAssistant) -> None:
    """An add whose provider fails after its first item lists that item, and still fails."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2"])
    provider.add_error = ResourceTemporarilyUnavailable("rate limited")
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        assert await _listed(mass, playlist) == ["t1", "t2"]
        with pytest.raises(ResourceTemporarilyUnavailable):
            await _add(mass, playlist, "t3", "t4")
        assert await _listed(mass, playlist) == ["t1", "t2", "t3"]


async def test_an_add_after_a_removal_does_not_join_a_read_from_before_it(
    mass: MusicAssistant,
) -> None:
    """An add right after a removal reads the playlist itself, not through a read from before."""
    provider = _EditablePlaylistProvider(mass, ["t1", "t2", "t3"])
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        # a listing reads the first page as it is before the removal, and gets it later
        provider.gate.clear()
        listing = asyncio.create_task(_listed(mass, playlist))
        await _wait_for(lambda: provider.pages_fetched == 1)
        await _remove(mass, playlist, 2)
        add = asyncio.create_task(_add(mass, playlist, "t2"))
        # let the scan of the add reach the first page before the listing gets it
        for _ in range(50):
            if provider.pages_fetched == 2:
                break
            await asyncio.sleep(0.01)
        provider.gate.set()
        await asyncio.gather(listing, add)
    assert provider.track_ids == ["t1", "t3", "t2"]


async def test_a_failing_forget_still_drops_the_listing_and_announces_the_playlist(
    mass: MusicAssistant,
) -> None:
    """The listing is dropped and the playlist announced even when its pages can not be."""
    provider = _ForgetFailingProvider(mass, ["t1", "t2"])
    listing_uri = create_uri(MediaType.PLAYLIST, INSTANCE, PLAYLIST_ID)
    with _with_provider(mass, provider):
        playlist = await _library_playlist(mass)
        await _listed(mass, playlist)
        assert await mass.cache.get(
            ListingType.PLAYLIST_TRACKS.value,
            provider=listing_uri,
            category=CACHE_CATEGORY_LISTINGS,
        )
        with (
            patch.object(mass, "signal_event") as signal_event,
            pytest.raises(RuntimeError, match="not available"),
        ):
            await _add(mass, playlist, "t3")
    assert media_item_events(signal_event) == [EventType.MEDIA_ITEM_UPDATED]
    assert (
        await mass.cache.get(
            ListingType.PLAYLIST_TRACKS.value,
            provider=listing_uri,
            category=CACHE_CATEGORY_LISTINGS,
        )
        is None
    )
