"""Tests for the beat schedule refresh a Sendspin flow timeline rebase schedules."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    import pytest
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
    # Both are read-only properties on the real class.
    # One mock for the life of the player: the tests assert on what was pushed to it.
    monkeypatch.setattr(
        SendspinPlayer,
        "_visualizer_role",
        property(lambda _self: _self._role_mock),
        raising=False,
    )
    monkeypatch.setattr(SendspinPlayer, "synced_to", property(lambda _self: None), raising=False)
    monkeypatch.setattr(SendspinPlayer, "player_id", "leader", raising=False)
    # Re-read after the analysis await to detect a track change that raced with it.
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
    # Harness-only attributes the monkeypatched properties read back; the real class
    # declares neither, so they are set through an untyped view of the instance.
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
    monkeypatch.setattr(SendspinPlayer, "_flow_track_offset_us", staticmethod(lambda _d, _i: None))
    return player


async def test_a_track_change_during_the_analysis_await_drops_the_stale_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A refresh whose media changed under it must not publish the item it captured.

    The refresh a rebase schedules carries its own task id, so a media update does not
    cancel it: it can still be inside get_audio_analysis() when the track changes. Left
    unguarded it would overwrite the new track's schedule and then claim the old item in
    _last_beat_queue_item_id, which makes the re-push guard swallow the correction.
    """
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
        cast("object", SimpleNamespace(queue_id="q1")),  # type: ignore[arg-type]
        _queue_item(),
        _PROGRESS_MS,
        True,
    )

    # The publish path clears the role's schedule before writing the new one, so an
    # untouched role is proof this run stopped before it could overwrite item-2's.
    visualizer_role.clear_beat_schedule.assert_not_called()
    # Still naming the item that is actually published, so the next refresh for item-2
    # is not mistaken for a re-push of it.
    assert player._last_beat_queue_item_id == _QUEUE_ITEM_ID
    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US
