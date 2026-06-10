"""
Tests for the player-queue artist track selection (``get_artist_tracks``).

A library-only artist (no streaming provider mappings) stands in for a file-only
library: with the bare ``mass`` fixture (no streaming/metadata providers loaded),
its top-tracks and per-provider discography legs resolve to nothing, so only the
in-library leg can yield tracks. The integration tests use that fixture (a real
MusicAssistant with a real SQLite database); the unit tests at the bottom exercise
the multi-source union/dedup directly with mocked sources.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import (
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    PlayerQueuesController,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _library_mapping() -> set[ProviderMapping]:
    """Create a single library provider mapping with a unique provider item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=True,
        )
    }


async def _add_artist(mass: MusicAssistant, name: str) -> Artist:
    """Add a minimal library artist and return the stored item."""
    return await mass.music.artists.add_item_to_library(
        Artist(item_id="0", provider="library", name=name, provider_mappings=_library_mapping())
    )


async def _add_track(mass: MusicAssistant, name: str, artists: list[Artist]) -> Track:
    """Add a minimal library track credited to the given artists and return the stored item."""
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings=_library_mapping(),
            artists=UniqueList(artists),
        )
    )
    return await mass.music.tracks.get_library_item(added.item_id)


def _select(mass: MusicAssistant, value: str) -> None:
    """Set the artist enqueue-selection option."""
    mass.config.set_raw_core_config_value(
        "player_queues", CONF_DEFAULT_ENQUEUE_SELECT_ARTIST, value
    )


async def test_default_resolves_library_only_artist(mass: MusicAssistant) -> None:
    """The default option resolves a library-only artist to its in-library tracks."""
    artist = await _add_artist(mass, "ABBA")
    track_a = await _add_track(mass, "Dancing Queen", [artist])
    track_b = await _add_track(mass, "Mamma Mia", [artist])

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track_a.name, track_b.name}


async def test_prefer_library_returns_library_tracks(mass: MusicAssistant) -> None:
    """prefer_library returns the artist's in-library tracks when present."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "Waterloo", [artist])
    _select(mass, "prefer_library")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_legacy_all_album_tracks_maps_to_all_tracks(mass: MusicAssistant) -> None:
    """A stored legacy 'all_album_tracks' resolves like 'all_tracks' (no empty result)."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "SOS", [artist])
    _select(mass, "all_album_tracks")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_legacy_library_album_tracks_maps_to_library_tracks(mass: MusicAssistant) -> None:
    """A stored legacy 'library_album_tracks' resolves like 'library_tracks'."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "Fernando", [artist])
    _select(mass, "library_album_tracks")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_top_tracks_excludes_library_tracks(mass: MusicAssistant) -> None:
    """top_tracks returns only provider top tracks, never the in-library tracks."""
    artist = await _add_artist(mass, "ABBA")
    await _add_track(mass, "Honey Honey", [artist])
    _select(mass, "top_tracks")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert "Honey Honey" not in {t.name for t in result}


# --- unit tests for the all_tracks union and discography helper (mocked, no DB) ---


def _track_obj(name: str, version: str = "") -> Track:
    """Create a minimal track with the given name and version (for dedup-key tests)."""
    return Track(item_id=name, provider="test", name=name, version=version, provider_mappings=set())


def _album_obj(name: str, version: str = "") -> Album:
    """Create a minimal album with the given name and version (for dedup-key tests)."""
    return Album(item_id=name, provider="test", name=name, version=version, provider_mappings=set())


def _prov_mapping(instance: str, item_id: str) -> ProviderMapping:
    """Create a provider mapping for the given (streaming) provider instance."""
    return ProviderMapping(
        item_id=item_id, provider_domain="test", provider_instance=instance, in_library=False
    )


def _artist_obj(mappings: set[ProviderMapping] | None = None) -> Artist:
    """Create a minimal library artist for the resolution unit tests."""
    return Artist(item_id="1", provider="library", name="ABBA", provider_mappings=mappings or set())


def _fake_queues(selection: str) -> MagicMock:
    """Create a mock standing in for the controller, with the artist option preselected."""
    fake = MagicMock()
    fake.mass.config.get_raw_core_config_value = MagicMock(return_value=selection)
    return fake


async def test_all_tracks_unions_and_dedups_sources() -> None:
    """all_tracks merges the three sources and deduplicates by name+version."""
    fake = _fake_queues("all_tracks")
    fake._library_artist_tracks = AsyncMock(return_value=[_track_obj("Shared")])
    fake.mass.music.artists.top_tracks = AsyncMock(
        return_value=[_track_obj("Shared"), _track_obj("TopOnly")]
    )
    fake._provider_discography_tracks = AsyncMock(return_value=[_track_obj("DiscOnly")])

    result = await PlayerQueuesController.get_artist_tracks(
        cast("PlayerQueuesController", fake), _artist_obj()
    )

    assert sorted(t.name for t in result) == ["DiscOnly", "Shared", "TopOnly"]


async def test_all_tracks_keeps_distinct_versions() -> None:
    """Tracks sharing a name but differing in version are both kept."""
    fake = _fake_queues("all_tracks")
    fake._library_artist_tracks = AsyncMock(return_value=[_track_obj("Song", "")])
    fake.mass.music.artists.top_tracks = AsyncMock(return_value=[_track_obj("Song", "Remix")])
    fake._provider_discography_tracks = AsyncMock(return_value=[])

    result = await PlayerQueuesController.get_artist_tracks(
        cast("PlayerQueuesController", fake), _artist_obj()
    )

    assert sorted(t.version for t in result) == ["", "Remix"]


async def test_all_tracks_survives_a_failing_source() -> None:
    """A failing source (e.g. a flaky provider) is dropped; the other sources still resolve."""
    fake = _fake_queues("all_tracks")
    fake._library_artist_tracks = AsyncMock(return_value=[_track_obj("Local")])
    fake.mass.music.artists.top_tracks = AsyncMock(side_effect=RuntimeError("provider down"))
    fake._provider_discography_tracks = AsyncMock(return_value=[_track_obj("Disc")])

    result = await PlayerQueuesController.get_artist_tracks(
        cast("PlayerQueuesController", fake), _artist_obj()
    )

    assert {t.name for t in result} == {"Local", "Disc"}
    fake.logger.warning.assert_called_once()


async def test_prefer_library_falls_back_to_top_tracks() -> None:
    """prefer_library falls back to top tracks when the artist has no in-library tracks."""
    fake = _fake_queues("prefer_library")
    fake._library_artist_tracks = AsyncMock(return_value=[])
    fake.mass.music.artists.top_tracks = AsyncMock(return_value=[_track_obj("Top")])

    result = await PlayerQueuesController.get_artist_tracks(
        cast("PlayerQueuesController", fake), _artist_obj()
    )

    assert {t.name for t in result} == {"Top"}


async def test_provider_discography_dedups_albums_across_providers() -> None:
    """_provider_discography_tracks deduplicates albums shared across providers by name+version."""
    fake = _fake_queues("all_tracks")
    artist = _artist_obj({_prov_mapping("p1", "a1"), _prov_mapping("p2", "a2")})
    fake.mass.music.get_unique_providers = MagicMock(return_value=["p1", "p2"])
    fake.mass.music.artists.get_provider_artist_albums = AsyncMock(
        return_value=[_album_obj("Arrival")]
    )
    fake.mass.music.albums.tracks = AsyncMock(return_value=[_track_obj("Dancing Queen")])

    result = await PlayerQueuesController._provider_discography_tracks(
        cast("PlayerQueuesController", fake), artist
    )

    assert [t.name for t in result] == ["Dancing Queen"]
    # both providers list the same album -> deduped -> a single album-tracks fetch
    assert fake.mass.music.albums.tracks.await_count == 1
