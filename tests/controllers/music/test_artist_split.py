"""Tests for repairing library artists a streaming provider knows under several ids."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from music_assistant_models.enums import EventType, ExternalID
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.constants import DB_TABLE_SETTINGS
from music_assistant.controllers.music.constants import SETTING_ARTIST_SPLIT_DONE
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INSTANCE = "tidal1"
DOMAIN = "tidal"


class _StubProvider(MusicProvider):
    """Minimal streaming provider serving fixed artists/tracks by provider item id."""

    def __init__(self, is_streaming: bool = True) -> None:
        """
        Initialize the stub provider without going through Provider.__init__.

        :param is_streaming: Value returned by is_streaming_provider.
        """
        self.config = MagicMock()
        self.config.instance_id = INSTANCE
        self.manifest = MagicMock()
        self.manifest.domain = DOMAIN
        self.logger = MagicMock()
        self.available = True
        self._is_streaming = is_streaming
        self.artists_by_id: dict[str, Artist] = {}
        self.tracks_by_id: dict[str, Track] = {}
        self.artist_errors: dict[str, Exception] = {}

    @property
    def is_streaming_provider(self) -> bool:
        """Return whether this stub behaves as a streaming provider."""
        return self._is_streaming

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return the prepared artist for the given id, or raise if unknown/erroring."""
        if prov_artist_id in self.artist_errors:
            raise self.artist_errors[prov_artist_id]
        if prov_artist_id not in self.artists_by_id:
            raise MediaNotFoundError(prov_artist_id)
        return self.artists_by_id[prov_artist_id]

    async def get_track(self, prov_track_id: str) -> Track:
        """Return the prepared track for the given id."""
        if prov_track_id not in self.tracks_by_id:
            raise MediaNotFoundError(prov_track_id)
        return self.tracks_by_id[prov_track_id]


def _mapping(item_id: str) -> ProviderMapping:
    """Create a provider mapping on the shared test instance."""
    return ProviderMapping(
        item_id=item_id, provider_domain=DOMAIN, provider_instance=INSTANCE, in_library=True
    )


def _prov_artist(
    item_id: str, name: str, external_ids: set[tuple[ExternalID, str]] | None = None
) -> Artist:
    """Build an Artist as the stub provider would return it."""
    return Artist(
        item_id=item_id,
        provider=INSTANCE,
        name=name,
        external_ids=external_ids or set(),
        provider_mappings={_mapping(item_id)},
    )


def _prov_track(item_id: str, artist: Artist, name: str = "Track") -> Track:
    """Build a Track as the stub provider would return it, credited to the given artist."""
    return Track(
        item_id=item_id,
        provider=INSTANCE,
        name=name,
        provider_mappings={_mapping(item_id)},
        artists=UniqueList([artist]),
    )


async def _add_merged_artist(
    mass: MusicAssistant, name: str = "Loud"
) -> tuple[Artist, _StubProvider]:
    """Add a library artist that already carries two mappings ("1" and "2") on the instance."""
    provider = _StubProvider()
    mass._providers[INSTANCE] = provider
    library_artist = await mass.music.artists.add_item_to_library(
        Artist(item_id="1", provider=INSTANCE, name=name, provider_mappings={_mapping("1")})
    )
    await mass.music.artists.add_provider_mappings(library_artist.item_id, [_mapping("2")])
    return library_artist, provider


async def test_split_relinks_tracks_to_the_right_artist(mass: MusicAssistant) -> None:
    """Two provider ids merged under one artist are split and tracks re-linked correctly."""
    library_artist, provider = await _add_merged_artist(mass)
    artist1 = _prov_artist("1", "Loud")
    artist2 = _prov_artist("2", "Loud")
    provider.artists_by_id = {"1": artist1, "2": artist2}

    track1 = _prov_track("t1", artist1, "Track One")
    track2 = _prov_track("t2", artist2, "Track Two")
    provider.tracks_by_id = {"t1": track1, "t2": track2}
    db_track1 = await mass.music.tracks.add_item_to_library(track1)
    db_track2 = await mass.music.tracks.add_item_to_library(track2)

    await mass.music.artists.split_merged_provider_artists()

    all_artists = await mass.music.artists.library_items(limit=500, summary=False)
    assert len(all_artists) == 2
    original = await mass.music.artists.get_library_item(library_artist.item_id)
    assert {m.item_id for m in original.provider_mappings if m.provider_instance == INSTANCE} == {
        "1"
    }
    split_off = next(a for a in all_artists if a.item_id != original.item_id)
    assert {m.item_id for m in split_off.provider_mappings if m.provider_instance == INSTANCE} == {
        "2"
    }

    refreshed_track1 = await mass.music.tracks.get_library_item(db_track1.item_id)
    refreshed_track2 = await mass.music.tracks.get_library_item(db_track2.item_id)
    assert int(refreshed_track1.artists[0].item_id) == int(original.item_id)
    assert int(refreshed_track2.artists[0].item_id) == int(split_off.item_id)


async def test_split_removes_dead_mapping(mass: MusicAssistant) -> None:
    """A mapping the provider no longer knows about is dropped, no split-off artist created."""
    library_artist, provider = await _add_merged_artist(mass)
    provider.artists_by_id = {"1": _prov_artist("1", "Loud")}

    await mass.music.artists.split_merged_provider_artists()

    all_artists = await mass.music.artists.library_items(limit=500, summary=False)
    assert len(all_artists) == 1
    refreshed = await mass.music.artists.get_library_item(library_artist.item_id)
    assert {m.item_id for m in refreshed.provider_mappings if m.provider_instance == INSTANCE} == {
        "1"
    }


async def test_split_keeps_artists_sharing_an_external_id(mass: MusicAssistant) -> None:
    """A shared MusicBrainz artist id marks the second mapping as a legitimate link."""
    library_artist, provider = await _add_merged_artist(mass)
    mb_id = {(ExternalID.MB_ARTIST, "same-artist")}
    provider.artists_by_id = {
        "1": _prov_artist("1", "Loud", mb_id),
        "2": _prov_artist("2", "Loud", mb_id),
    }

    await mass.music.artists.split_merged_provider_artists()

    all_artists = await mass.music.artists.library_items(limit=500, summary=False)
    assert len(all_artists) == 1
    refreshed = await mass.music.artists.get_library_item(library_artist.item_id)
    assert {m.item_id for m in refreshed.provider_mappings if m.provider_instance == INSTANCE} == {
        "1",
        "2",
    }


async def test_split_ignores_non_streaming_provider(mass: MusicAssistant) -> None:
    """A provider whose ids are not stable (is_streaming_provider False) is left alone."""
    provider = _StubProvider(is_streaming=False)
    mass._providers[INSTANCE] = provider
    library_artist = await mass.music.artists.add_item_to_library(
        Artist(item_id="1", provider=INSTANCE, name="Loud", provider_mappings={_mapping("1")})
    )
    await mass.music.artists.add_provider_mappings(library_artist.item_id, [_mapping("2")])

    await mass.music.artists.split_merged_provider_artists()

    all_artists = await mass.music.artists.library_items(limit=500, summary=False)
    assert len(all_artists) == 1
    refreshed = await mass.music.artists.get_library_item(library_artist.item_id)
    assert {m.item_id for m in refreshed.provider_mappings if m.provider_instance == INSTANCE} == {
        "1",
        "2",
    }


async def test_split_skips_artist_on_unexpected_error(mass: MusicAssistant) -> None:
    """A non-MediaNotFoundError failure fetching one mapping skips the whole artist."""
    library_artist, provider = await _add_merged_artist(mass)
    provider.artist_errors = {"1": RuntimeError("boom")}
    provider.artists_by_id = {"2": _prov_artist("2", "Loud")}

    await mass.music.artists.split_merged_provider_artists()

    all_artists = await mass.music.artists.library_items(limit=500, summary=False)
    assert len(all_artists) == 1
    refreshed = await mass.music.artists.get_library_item(library_artist.item_id)
    assert {m.item_id for m in refreshed.provider_mappings if m.provider_instance == INSTANCE} == {
        "1",
        "2",
    }


async def test_split_marks_itself_done_after_a_completed_pass(mass: MusicAssistant) -> None:
    """A pass that runs to completion writes the done marker to the settings table."""
    _, provider = await _add_merged_artist(mass)
    provider.artists_by_id = {
        "1": _prov_artist("1", "Loud"),
        "2": _prov_artist("2", "Loud"),
    }

    assert await mass.music.artists._artist_split_done() is False
    await mass.music.artists.split_merged_provider_artists()
    assert await mass.music.artists._artist_split_done() is True


async def test_split_still_runs_when_marker_is_already_set(mass: MusicAssistant) -> None:
    """The done marker gates the automatic trigger only, not a direct call to the pass."""
    await mass.music.database.insert_or_replace(
        DB_TABLE_SETTINGS, {"key": SETTING_ARTIST_SPLIT_DONE, "value": "1", "type": "bool"}
    )
    library_artist, provider = await _add_merged_artist(mass)
    artist1 = _prov_artist("1", "Loud")
    artist2 = _prov_artist("2", "Loud")
    provider.artists_by_id = {"1": artist1, "2": artist2}
    # a track credited to mapping "2" is what actually causes a split-off artist to be
    # created (see test_split_relinks_tracks_to_the_right_artist); without one, removing
    # the surplus mapping alone leaves a single artist behind
    track2 = _prov_track("t2", artist2, "Track Two")
    provider.tracks_by_id = {"t2": track2}
    await mass.music.tracks.add_item_to_library(track2)

    await mass.music.artists.split_merged_provider_artists()

    all_artists = await mass.music.artists.library_items(limit=500, summary=False)
    assert len(all_artists) == 2
    original = await mass.music.artists.get_library_item(library_artist.item_id)
    assert {m.item_id for m in original.provider_mappings if m.provider_instance == INSTANCE} == {
        "1"
    }


async def test_sync_completed_triggers_split_once_then_stops_listening(
    mass: MusicAssistant,
) -> None:
    """With no marker set, the first sync-completed event triggers the split and unsubscribes."""
    with patch.object(mass.music.artists, "split_merged_provider_artists") as mock_split:
        await mass.music.artists._on_music_sync_completed(MagicMock())
        mock_split.assert_called_once()

        # the handler unsubscribed itself, so a further event does not call it again
        mass.signal_event(EventType.MUSIC_SYNC_COMPLETED)
        await asyncio.sleep(0)
        mock_split.assert_called_once()


async def test_sync_completed_does_not_trigger_when_marker_is_set(mass: MusicAssistant) -> None:
    """With the done marker already set, the sync-completed handler does not trigger the split."""
    await mass.music.database.insert_or_replace(
        DB_TABLE_SETTINGS, {"key": SETTING_ARTIST_SPLIT_DONE, "value": "1", "type": "bool"}
    )
    with patch.object(mass.music.artists, "split_merged_provider_artists") as mock_split:
        await mass.music.artists._on_music_sync_completed(MagicMock())
        mock_split.assert_not_called()


async def test_split_merged_artists_command_triggers_regardless_of_marker(
    mass: MusicAssistant,
) -> None:
    """The manual API command triggers the split even when the done marker is set."""
    await mass.music.database.insert_or_replace(
        DB_TABLE_SETTINGS, {"key": SETTING_ARTIST_SPLIT_DONE, "value": "1", "type": "bool"}
    )
    with patch.object(mass.music.artists, "split_merged_provider_artists") as mock_split:
        await mass.music.split_merged_artists()
        await asyncio.sleep(0)
        mock_split.assert_called_once()


async def test_split_noop_while_lock_is_held(mass: MusicAssistant) -> None:
    """A concurrent pass already holding the lock leaves a mergeable artist untouched."""
    library_artist, provider = await _add_merged_artist(mass)
    provider.artists_by_id = {
        "1": _prov_artist("1", "Loud"),
        "2": _prov_artist("2", "Loud"),
    }

    lock = mass.music.artists._split_merged_artists_lock
    await lock.acquire()
    try:
        await mass.music.artists.split_merged_provider_artists()
    finally:
        lock.release()

    all_artists = await mass.music.artists.library_items(limit=500, summary=False)
    assert len(all_artists) == 1
    refreshed = await mass.music.artists.get_library_item(library_artist.item_id)
    assert {m.item_id for m in refreshed.provider_mappings if m.provider_instance == INSTANCE} == {
        "1",
        "2",
    }
