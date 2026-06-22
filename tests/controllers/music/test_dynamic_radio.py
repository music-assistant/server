"""Tests for MusicController.get_dynamic_radio_tracks."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.controllers.music import (
    RADIO_TRACK_MAX_DURATION_SECS,
    MusicController,
)


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
    item.uri = f"test://{media_type.value}/{item_id}"
    return item


def _make_controller(
    base_tracks_by_seed: dict[str, list[MagicMock]], similar: list[MagicMock]
) -> MusicController:
    """Build a MusicController with the bits required by get_dynamic_radio_tracks stubbed out."""
    ctrl = MusicController.__new__(MusicController)
    tracks_ctrl = MagicMock()
    tracks_ctrl.similar_tracks = AsyncMock(return_value=similar)
    ctrl.tracks = tracks_ctrl

    media_controllers: dict[MediaType, MagicMock] = {}

    def get_controller(media_type: MediaType) -> MagicMock:
        if media_type not in media_controllers:
            media_ctrl = MagicMock()
            media_ctrl.radio_mode_base_tracks = AsyncMock(
                side_effect=lambda seed, _prefs=None: base_tracks_by_seed.get(seed.item_id, [])
            )
            media_controllers[media_type] = media_ctrl
        return media_controllers[media_type]

    ctrl.get_controller = get_controller  # type: ignore[method-assign]
    return ctrl


@pytest.mark.asyncio
async def test_no_seeds_raises() -> None:
    """An empty seed list raises UnsupportedFeaturedException."""
    ctrl = _make_controller({}, [])
    with pytest.raises(UnsupportedFeaturedException):
        await ctrl.get_dynamic_radio_tracks([])


@pytest.mark.asyncio
async def test_seed_with_no_base_tracks_raises() -> None:
    """When all seeds yield zero base tracks, raises UnsupportedFeaturedException."""
    seed = _seed(MediaType.ALBUM, "1")
    ctrl = _make_controller({"1": []}, [])
    with pytest.raises(UnsupportedFeaturedException):
        await ctrl.get_dynamic_radio_tracks([seed])


@pytest.mark.asyncio
async def test_long_tracks_are_filtered() -> None:
    """Tracks longer than the max-duration threshold are dropped from candidates."""
    seed = _seed(MediaType.TRACK, "s1")
    base = _track("s1")
    short = _track("short", duration=100)
    long_track = _track("long", duration=RADIO_TRACK_MAX_DURATION_SECS + 1)
    ctrl = _make_controller({"s1": [base]}, [short, long_track])

    result = await ctrl.get_dynamic_radio_tracks([seed], target_size=10)
    ids = {t.item_id for t in result}
    assert "short" in ids
    assert "long" not in ids


@pytest.mark.asyncio
async def test_include_base_tracks_emits_seed_track() -> None:
    """include_base_tracks=True ensures at least one base track is in the output."""
    seed = _seed(MediaType.TRACK, "s1")
    base = _track("s1")
    similar = [_track(f"sim{i}") for i in range(5)]
    ctrl = _make_controller({"s1": [base]}, similar)

    result = await ctrl.get_dynamic_radio_tracks([seed], include_base_tracks=True, target_size=5)
    assert any(t.item_id == "s1" for t in result)


@pytest.mark.asyncio
async def test_exclude_base_tracks_omits_seed_track() -> None:
    """include_base_tracks=False keeps only similar tracks in the output."""
    seed = _seed(MediaType.TRACK, "s1")
    base = _track("s1")
    similar = [_track(f"sim{i}") for i in range(5)]
    ctrl = _make_controller({"s1": [base]}, similar)

    result = await ctrl.get_dynamic_radio_tracks([seed], include_base_tracks=False, target_size=5)
    assert all(t.item_id != "s1" for t in result)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_multiple_seeds_dedup_base_tracks() -> None:
    """Duplicate base tracks across seeds are deduplicated before sampling."""
    seed_a = _seed(MediaType.ALBUM, "a")
    seed_b = _seed(MediaType.ALBUM, "b")
    shared = _track("shared")
    ctrl = _make_controller(
        {"a": [shared, _track("a-only")], "b": [shared, _track("b-only")]},
        [_track("sim1"), _track("sim2")],
    )

    await ctrl.get_dynamic_radio_tracks([seed_a, seed_b], target_size=5)
    # 3 unique base tracks after dedup. similar_tracks fires once per base per allow_lookup pass;
    # with only 2 similar mocks per call we never hit the dynamic-target threshold, so both
    # passes run → 3 * 2 = 6 calls. Without dedup it would be 4 * 2 = 8.
    assert cast("Any", ctrl.tracks.similar_tracks).call_count <= 6
