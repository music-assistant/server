"""Tests for the Autoplay helper and the per-queue autoplay/crossfade config entries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import CrossfadeMode
from music_assistant_models.media_items import Playlist, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.autoplay import (
    AUTOPLAY_BATCH_SIZE,
    AutoplayHelper,
    AutoplayMode,
)

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


def _track(item_id: str, *, provider: str = "library", available: bool = True) -> Track:
    """Build a minimal Track for the given id/provider (availability via its provider mapping)."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
                available=available,
            )
        },
    )


def _playlist(item_id: str = "pl1") -> Playlist:
    """Build a minimal Playlist."""
    return Playlist(
        item_id=item_id,
        provider="library",
        name="My Playlist",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="library", provider_instance="library")
        },
    )


def _helper() -> tuple[AutoplayHelper, MagicMock]:
    """
    Build an AutoplayHelper and return it together with its mocked controller.

    The returned MagicMock stands in for the owning controller; configure expectations and
    make assertions through it (its attributes are untyped, so mock wiring stays simple).
    """
    queues = MagicMock()
    return AutoplayHelper(queues), queues


def _queue(*seeds: Track, queue_id: str = "q1") -> PlayerQueue:
    """Build a queue stand-in with the given enqueued media items."""
    return cast(
        "PlayerQueue",
        SimpleNamespace(queue_id=queue_id, display_name="Queue", enqueued_media_items=list(seeds)),
    )


# --- resolve_mode ---


def test_resolve_mode_reads_config() -> None:
    """A stored mode value is parsed into the matching enum."""
    helper, queues = _helper()
    queues.mass.config.get_raw_player_queue_config_value = MagicMock(return_value="library")
    assert helper.resolve_mode("q1") == AutoplayMode.LIBRARY


def test_resolve_mode_defaults_to_auto() -> None:
    """An unknown/blank value falls back to the AUTO mode."""
    helper, queues = _helper()
    queues.mass.config.get_raw_player_queue_config_value = MagicMock(return_value="bogus")
    assert helper.resolve_mode("q1") == AutoplayMode.AUTO


# --- get_library_tracks ---


async def test_get_library_tracks_genre_then_topup() -> None:
    """Genre-matched tracks come first, then a whole-library top-up, capped to the batch size."""
    helper, queues = _helper()
    queues.mass.config.get_raw_player_queue_config_value = MagicMock(return_value="library")
    queues.mass.music.genres.get_genres_for_media_item = AsyncMock(
        return_value=[SimpleNamespace(item_id="10")]
    )
    genre_tracks = [_track("g1"), _track("g2")]
    topup_tracks = [_track(f"r{i}") for i in range(30)]
    queues.mass.music.tracks.library_items = AsyncMock(side_effect=[genre_tracks, topup_tracks])

    result = await helper.get_library_tracks(_queue(_track("seed")), exclude=set())

    # genre query used random_play_count ordering and the genre id we collected
    first_call = queues.mass.music.tracks.library_items.await_args_list[0]
    assert first_call.kwargs["order_by"] == "random_play_count"
    assert first_call.kwargs["genre"] == [10]
    # genre matches lead, batch size is respected
    assert result[:2] == genre_tracks
    assert len(result) == AUTOPLAY_BATCH_SIZE


async def test_get_library_tracks_without_genre_uses_random_mix() -> None:
    """With no genre signal, only the whole-library random mix is queried."""
    helper, queues = _helper()
    queues.mass.music.genres.get_genres_for_media_item = AsyncMock(return_value=[])
    queues.mass.music.tracks.library_items = AsyncMock(return_value=[_track("r1"), _track("r2")])

    result = await helper.get_library_tracks(_queue(_track("seed")), exclude=set())

    queues.mass.music.tracks.library_items.assert_awaited_once()
    assert "genre" not in queues.mass.music.tracks.library_items.await_args.kwargs
    assert {t.item_id for t in result} == {"r1", "r2"}


async def test_get_library_tracks_excludes_and_dedupes() -> None:
    """Already-queued, duplicate and unavailable tracks are filtered out."""
    helper, queues = _helper()
    queues.mass.music.genres.get_genres_for_media_item = AsyncMock(return_value=[])
    dupe = _track("dupe")
    excluded = _track("excluded")
    gone = _track("gone", available=False)
    queues.mass.music.tracks.library_items = AsyncMock(
        return_value=[dupe, dupe, excluded, gone, _track("fresh")]
    )

    result = await helper.get_library_tracks(_queue(), exclude={excluded})

    assert [t.item_id for t in result] == ["dupe", "fresh"]


async def test_get_library_tracks_tops_up_past_unavailable_genre_matches() -> None:
    """A genre batch that looks full but is all unavailable still triggers the library top-up."""
    helper, queues = _helper()
    queues.mass.music.genres.get_genres_for_media_item = AsyncMock(
        return_value=[SimpleNamespace(item_id="1")]
    )
    genre_tracks = [_track(f"g{i}", available=False) for i in range(AUTOPLAY_BATCH_SIZE)]
    topup_tracks = [_track(f"r{i}") for i in range(AUTOPLAY_BATCH_SIZE)]
    queues.mass.music.tracks.library_items = AsyncMock(side_effect=[genre_tracks, topup_tracks])

    result = await helper.get_library_tracks(_queue(_track("seed")), exclude=set())

    assert queues.mass.music.tracks.library_items.await_count == 2
    assert len(result) == AUTOPLAY_BATCH_SIZE
    assert all(track.available for track in result)


# --- get_playlist_tracks ---


async def test_get_playlist_tracks_returns_filtered_batch() -> None:
    """The configured playlist's tracks are returned minus the excluded ones."""
    helper, queues = _helper()
    queues.mass.config.get_raw_player_queue_config_value = MagicMock(
        return_value="library://playlist/pl1"
    )
    queues.mass.music.get_item_by_uri = AsyncMock(return_value=_playlist())
    excluded = _track("t2")
    queues.get_playlist_tracks = AsyncMock(return_value=[_track("t1"), excluded, _track("t3")])

    result = await helper.get_playlist_tracks(_queue(), exclude={excluded})

    assert {t.item_id for t in result} == {"t1", "t3"}


async def test_get_playlist_tracks_without_config_returns_empty() -> None:
    """Playlist mode with no configured playlist yields nothing (and does not crash)."""
    helper, queues = _helper()
    queues.mass.config.get_raw_player_queue_config_value = MagicMock(return_value=None)

    assert await helper.get_playlist_tracks(_queue(), exclude=set()) == []


# --- config entries ---


def test_get_queue_config_entries_categories_and_dependencies() -> None:
    """Autoplay and crossfade entries land in their own categories with the playlist depends_on."""
    fake = MagicMock()
    fake.mass.music.providers = []
    fake.mass.streams.smart_fades_available = False

    entries = PlayerQueuesController.get_queue_config_entries(cast("PlayerQueuesController", fake))
    by_key = {entry.key: entry for entry in entries}

    assert by_key["autoplay_mode"].category == "autoplay"
    assert by_key["autoplay_mode"].default_value == AutoplayMode.AUTO.value
    assert by_key["autoplay_playlist"].depends_on == "autoplay_mode"
    assert by_key["autoplay_playlist"].depends_on_value == AutoplayMode.PLAYLIST.value
    assert by_key["crossfade_mode"].category == "crossfade"
    assert by_key["crossfade_duration"].category == "crossfade"
    assert by_key["volume_normalization"].category == "audio"
    # the standard crossfade duration is a normal (non-advanced) setting that only applies when
    # the standard crossfade mode is selected, and is ordered after the mode entry
    assert by_key["crossfade_duration"].advanced is False
    assert by_key["crossfade_duration"].depends_on == "crossfade_mode"
    assert by_key["crossfade_duration"].depends_on_value == CrossfadeMode.STANDARD_CROSSFADE.value
    keys = [entry.key for entry in entries]
    assert keys.index("crossfade_mode") < keys.index("crossfade_duration")
    # the 'similar' option is disabled when no provider can supply similar tracks
    similar_option = next(
        opt for opt in by_key["autoplay_mode"].options if opt.value == AutoplayMode.SIMILAR.value
    )
    assert similar_option.disabled is True
