"""Tests for the radio_playlist provider's track generation (base + similar assembly)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.controllers.music.constants import RADIO_TRACK_MAX_DURATION_SECS
from music_assistant.providers.radio_playlist import RadioPlaylistProvider

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


def _track(item_id: str, duration: int = 200) -> MagicMock:
    track = MagicMock()
    track.item_id = item_id
    track.provider = "test"
    track.uri = f"test://track/{item_id}"
    track.name = f"Track {item_id}"
    track.duration = duration
    track.media_type = MediaType.TRACK
    return track


def _seed(media_type: MediaType, item_id: str) -> MagicMock:
    item = MagicMock()
    item.media_type = media_type
    item.item_id = item_id
    item.provider = "test"
    item.uri = f"test://{media_type.value}/{item_id}"
    return item


def _make_provider(
    base_tracks_by_seed: dict[str, list[MagicMock]], similar: list[MagicMock]
) -> RadioPlaylistProvider:
    """Build a provider with the queue's track resolution + similar lookups stubbed out."""
    prov = RadioPlaylistProvider.__new__(RadioPlaylistProvider)
    mass = MagicMock()
    mass.music.tracks.similar_tracks = AsyncMock(return_value=similar)
    mass.player_queues.get_tracks_for_playback = AsyncMock(
        side_effect=lambda item: base_tracks_by_seed.get(item.item_id, [])
    )
    prov.mass = cast("MusicAssistant", mass)
    return prov


@pytest.mark.asyncio
async def test_no_seeds_raises() -> None:
    """An empty seed list raises UnsupportedFeaturedException."""
    prov = _make_provider({}, [])
    with pytest.raises(UnsupportedFeaturedException):
        await prov.get_dynamic_tracks([])


@pytest.mark.asyncio
async def test_seed_with_no_base_tracks_raises() -> None:
    """When all seeds yield zero base tracks, raises UnsupportedFeaturedException."""
    seed = _seed(MediaType.ALBUM, "1")
    prov = _make_provider({"1": []}, [])
    with pytest.raises(UnsupportedFeaturedException):
        await prov.get_dynamic_tracks([seed])


@pytest.mark.asyncio
async def test_long_tracks_are_filtered() -> None:
    """Tracks longer than the max-duration threshold are dropped from candidates."""
    seed = _seed(MediaType.TRACK, "s1")
    base = _track("s1")
    short = _track("short", duration=100)
    long_track = _track("long", duration=RADIO_TRACK_MAX_DURATION_SECS + 1)
    prov = _make_provider({"s1": [base]}, [short, long_track])

    result = await prov.get_dynamic_tracks([seed], target_size=10)
    ids = {t.item_id for t in result}
    assert "short" in ids
    assert "long" not in ids


@pytest.mark.asyncio
async def test_include_base_tracks_emits_seed_track() -> None:
    """include_base_tracks=True ensures at least one base track is in the output."""
    seed = _seed(MediaType.TRACK, "s1")
    base = _track("s1")
    similar = [_track(f"sim{i}") for i in range(5)]
    prov = _make_provider({"s1": [base]}, similar)

    result = await prov.get_dynamic_tracks([seed], include_base_tracks=True, target_size=5)
    assert any(t.item_id == "s1" for t in result)


@pytest.mark.asyncio
async def test_exclude_base_tracks_omits_seed_track() -> None:
    """include_base_tracks=False keeps only similar tracks in the output."""
    seed = _seed(MediaType.TRACK, "s1")
    base = _track("s1")
    similar = [_track(f"sim{i}") for i in range(5)]
    prov = _make_provider({"s1": [base]}, similar)

    result = await prov.get_dynamic_tracks([seed], include_base_tracks=False, target_size=5)
    assert all(t.item_id != "s1" for t in result)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_multiple_seeds_dedup_base_tracks() -> None:
    """Duplicate base tracks across seeds are deduplicated before sampling."""
    seed_a = _seed(MediaType.ALBUM, "a")
    seed_b = _seed(MediaType.ALBUM, "b")
    shared = _track("shared")
    prov = _make_provider(
        {"a": [shared, _track("a-only")], "b": [shared, _track("b-only")]},
        [],
    )
    similar_tracks = cast("Any", prov.mass.music.tracks.similar_tracks)
    similar_tracks.side_effect = lambda item_id, _provider, **_kwargs: [
        _track(f"similar-{item_id}")
    ]

    await prov.get_dynamic_tracks([seed_a, seed_b], target_size=5)
    # Three unique base tracks remain after deduplication, each with a successful direct lookup.
    assert similar_tracks.call_count == 3


@pytest.mark.asyncio
async def test_cross_provider_lookup_only_retries_without_usable_results() -> None:
    """A cross-provider lookup is only used when the direct lookup has no usable result."""
    seed = _seed(MediaType.ALBUM, "seed")
    direct_base = _track("direct")
    fallback_base = _track("fallback")
    filtered_base = _track("filtered")
    prov = _make_provider({"seed": [direct_base, fallback_base, filtered_base]}, [])
    similar_tracks = cast("Any", prov.mass.music.tracks.similar_tracks)

    def get_similar(
        item_id: str, _provider: str, *, allow_lookup: bool, **_kwargs: Any
    ) -> list[MagicMock]:
        if item_id == "direct":
            return [_track("direct-result")]
        if item_id == "filtered":
            return (
                [_track("filtered-fallback")]
                if allow_lookup
                else [_track("too-long", RADIO_TRACK_MAX_DURATION_SECS + 1)]
            )
        if allow_lookup:
            return [_track("fallback-result")]
        return []

    similar_tracks.side_effect = get_similar

    result = await prov.get_dynamic_tracks([seed], target_size=5)

    lookups = [
        (call.args[0], call.kwargs["allow_lookup"]) for call in similar_tracks.await_args_list
    ]
    assert ("direct", False) in lookups
    assert ("direct", True) not in lookups
    assert ("fallback", False) in lookups
    assert ("fallback", True) in lookups
    assert ("filtered", False) in lookups
    assert ("filtered", True) in lookups
    assert {track.item_id for track in result} == {
        "direct-result",
        "fallback-result",
        "filtered-fallback",
    }


@pytest.mark.asyncio
async def test_artist_seed_uses_top_tracks_not_full_resolution() -> None:
    """An artist seed samples base tracks via top_tracks; other seeds still go through the queue."""
    artist_seed = _seed(MediaType.ARTIST, "art1")
    album_seed = _seed(MediaType.ALBUM, "alb1")
    prov = _make_provider({"alb1": [_track("albtrack1")]}, [])
    prov.mass.music.artists.top_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=[_track("top1")]
    )

    await prov.get_dynamic_tracks([artist_seed, album_seed], target_size=5)

    prov.mass.music.artists.top_tracks.assert_awaited_once_with("art1", "test")
    get_tracks_for_playback = cast("Any", prov.mass.player_queues.get_tracks_for_playback)
    called_item_ids = [call.args[0].item_id for call in get_tracks_for_playback.await_args_list]
    assert called_item_ids == ["alb1"]


@pytest.mark.asyncio
async def test_artist_seed_without_top_tracks_falls_back() -> None:
    """An artist whose providers yield no top tracks still seeds via full resolution."""
    artist_seed = _seed(MediaType.ARTIST, "art1")
    prov = _make_provider({"art1": [_track("full1")]}, [_track("sim1")])
    prov.mass.music.artists.top_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await prov.get_dynamic_tracks([artist_seed], include_base_tracks=True, target_size=5)

    get_tracks_for_playback = cast("Any", prov.mass.player_queues.get_tracks_for_playback)
    called_item_ids = [call.args[0].item_id for call in get_tracks_for_playback.await_args_list]
    assert called_item_ids == ["art1"]
    assert any(t.item_id == "full1" for t in result)


@pytest.mark.asyncio
async def test_similar_lookup_failure_keeps_base_tracks() -> None:
    """When no provider supports similar tracks, base tracks still carry the playlist."""
    seed = _seed(MediaType.TRACK, "s1")
    prov = _make_provider({"s1": [_track("s1")]}, [])
    cast("Any", prov.mass.music.tracks.similar_tracks).side_effect = UnsupportedFeaturedException(
        "no similar provider"
    )
    result = await prov.get_dynamic_tracks([seed], include_base_tracks=True, target_size=5)
    assert any(t.item_id == "s1" for t in result)
