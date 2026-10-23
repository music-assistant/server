"""Tests for the beat schedule refresh a Sendspin flow timeline rebase schedules."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.providers.sendspin.playback import SendspinPlaybackSession

_QUEUE_ITEM_ID = "qi-1"
_NOW_US = 50_000_000
_PUBLISHED_ANCHOR_US = 40_000_000
_PROGRESS_MS = 9_000


def _queue_item() -> QueueItem:
    return cast(
        "QueueItem",
        SimpleNamespace(
            queue_item_id=_QUEUE_ITEM_ID,
            streamdetails=SimpleNamespace(
                seek_position=0, item_id="t1", provider="test", media_type="track"
            ),
        ),
    )


def _player(monkeypatch: pytest.MonkeyPatch) -> SendspinPlayer:
    """Build a SendspinPlayer with only what _send_beat_schedule touches."""
    player = SendspinPlayer.__new__(SendspinPlayer)
    monkeypatch.setattr(
        SendspinPlayer,
        "_visualizer_role",
        property(lambda _self: _self._role_mock),
        raising=False,
    )
    monkeypatch.setattr(SendspinPlayer, "synced_to", property(lambda _self: None), raising=False)
    monkeypatch.setattr(SendspinPlayer, "player_id", "leader", raising=False)
    monkeypatch.setattr(
        SendspinPlayer,
        "state",
        property(lambda _self: SimpleNamespace(current_media=_self._live_media)),
        raising=False,
    )
    monkeypatch.setattr(
        SendspinPlayer,
        "provider",
        property(
            lambda _self: SimpleNamespace(
                server_api=SimpleNamespace(clock=SimpleNamespace(now_us=lambda: _NOW_US))
            )
        ),
        raising=False,
    )
    harness = cast("Any", player)
    harness._role_mock = MagicMock()
    harness._live_media = SimpleNamespace(queue_item_id=_QUEUE_ITEM_ID)
    player._last_beat_queue_item_id = _QUEUE_ITEM_ID
    player._last_beat_anchor_us = _PUBLISHED_ANCHOR_US
    player._beat_retry_task = None
    player._beat_retry_queue_item_id = None

    mass = MagicMock()
    mass.get_providers.return_value = [SimpleNamespace(available=True, domain="smart_fades")]
    mass.player_queues.queue_data_or_none.return_value = SimpleNamespace(flow_mode_stream_log=[])
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        return_value=SimpleNamespace(beats=[0.0, 1.0, 2.0], downbeats=[0.0])
    )
    player.mass = mass

    player.playback_session = cast(
        "SendspinPlaybackSession",
        SimpleNamespace(flow_track_anchor_us=lambda offset_us: _PUBLISHED_ANCHOR_US + offset_us),
    )
    return player


async def test_a_rebase_moves_the_published_beats_by_the_anchor_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebase republishes the playing track's beats shifted by the anchor delta."""
    player = _player(monkeypatch)
    harness = cast("Any", player)
    visualizer_role = cast("MagicMock", player._visualizer_role)
    delta_us = 1_500_000
    # The track plays from 10 s to 30 s into the flow stream, and its repeat is already buffered.
    old_timeline_start_us = _PUBLISHED_ANCHOR_US - 10_000_000
    harness.mass.player_queues.queue_data_or_none.return_value = SimpleNamespace(
        flow_mode_stream_log=[
            PlayLogEntry("qi-0", seconds_streamed=10.0),
            PlayLogEntry(_QUEUE_ITEM_ID, seconds_streamed=20.0),
            PlayLogEntry("qi-2", seconds_streamed=10.0),
            PlayLogEntry(_QUEUE_ITEM_ID),
        ]
    )
    player.playback_session = cast(
        "SendspinPlaybackSession",
        SimpleNamespace(
            flow_track_anchor_us=lambda offset_us: old_timeline_start_us + delta_us + offset_us
        ),
    )
    harness.mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        return_value=SimpleNamespace(beats=[float(b) for b in range(20)], downbeats=[0.0])
    )

    await player._send_beat_schedule(
        cast("PlayerQueue", SimpleNamespace(queue_id="q1")),
        _queue_item(),
        _PROGRESS_MS,
        True,
    )

    new_anchor_us = _PUBLISHED_ANCHOR_US + delta_us
    assert player._last_beat_anchor_us == new_anchor_us
    published = visualizer_role.append_beat_schedule.call_args.args[0]
    assert [beat.timestamp_us for beat in published] == list(
        range(50_500_000, 61_000_000, 1_000_000)
    )


async def test_a_track_change_during_the_analysis_await_drops_the_stale_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh whose track changed during the analysis await publishes nothing."""
    player = _player(monkeypatch)
    harness = cast("Any", player)
    visualizer_role = cast("MagicMock", player._visualizer_role)

    async def _analysis_then_track_change(*_args: object, **_kwargs: object) -> object:
        harness._live_media = SimpleNamespace(queue_item_id="item-2")
        return SimpleNamespace(beats=[0.0, 1.0, 2.0], downbeats=[0.0])

    harness.mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=_analysis_then_track_change
    )

    await player._send_beat_schedule(
        cast("PlayerQueue", SimpleNamespace(queue_id="q1")),
        _queue_item(),
        _PROGRESS_MS,
        True,
    )

    # Publishing always clears the schedule first.
    visualizer_role.clear_beat_schedule.assert_not_called()
    assert player._last_beat_queue_item_id == _QUEUE_ITEM_ID
    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US
