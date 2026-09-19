"""
Tests for the beat-schedule anchor when the Sendspin flow timeline is rebased.

Beat timings are published as absolute server-clock timestamps derived from the flow
timeline anchor, so a rebase leaves an already-published schedule pointing at the
pre-stall timeline. `_send_beat_schedule` normally recomputes the anchor from
`flow_track_anchor_us()`, but that needs `_flow_track_offset_us()` to place the current
track in the queue's flow log, and it cannot until the track appears there.

The fallback for that gap is `now_us - track_progress_ms * 1000`, and
`track_progress_ms` comes from the queue-backed `current_media`, whose position is
corrected asynchronously - MA treats a jump as discrete only at 1s, while Sendspin calls
a rebase significant at 500ms, so a rebase in between can refresh beats against a
position that has not caught up yet. These tests pin the signed-delta path that avoids
depending on it.
"""

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
# Between Sendspin's 500ms "significant rebase" and MA's 1s position-jump threshold, so
# the queue-backed position is not yet guaranteed to have been corrected.
_ANCHOR_DELTA_US = 750_000
# Deliberately stale: what the queue still reports, from before the rebase.
_STALE_PROGRESS_MS = 9_000


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


def _player(monkeypatch: pytest.MonkeyPatch, *, flow_offset_us: int | None) -> SendspinPlayer:
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
    player._pending_anchor_delta_us = 0
    player._anchor_rebase_pending = False
    player._beat_retry_task = None
    player._beat_retry_queue_item_id = None

    mass = MagicMock()
    # on_flow_timeline_rebased hands create_task a coroutine it never awaits here; close
    # it so the refresh does not run twice and no 'never awaited' warning is raised.
    mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
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
    # The flow log is what decides whether the preferred path is available at all.
    monkeypatch.setattr(
        SendspinPlayer, "_flow_track_offset_us", staticmethod(lambda _d, _i: flow_offset_us)
    )
    return player


def _report_rebase(player: SendspinPlayer, anchor_delta_us: int) -> None:
    """Drive the real rebase entry point, so its accumulation is under test too."""
    player.on_flow_timeline_rebased(anchor_delta_us)


async def _send(player: SendspinPlayer, *, anchor_delta_us: int) -> None:
    """Report a rebase of anchor_delta_us, then run the publish it would have scheduled."""
    if anchor_delta_us:
        _report_rebase(player, anchor_delta_us)
    await player._send_beat_schedule(
        cast("object", SimpleNamespace(queue_id="q1")),  # type: ignore[arg-type]
        _queue_item(),
        _STALE_PROGRESS_MS,
        True,
    )


async def test_rebase_shifts_the_published_anchor_when_the_flow_log_cannot_place_the_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no flow-log offset, the anchor moves by the timeline delta, not by progress."""
    player = _player(monkeypatch, flow_offset_us=None)

    await _send(player, anchor_delta_us=_ANCHOR_DELTA_US)

    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US + _ANCHOR_DELTA_US
    # Nothing derived from the stale queue position: that fallback would have anchored
    # at now - 9s, nowhere near the shifted anchor.
    assert player._last_beat_anchor_us != _NOW_US - _STALE_PROGRESS_MS * 1000


async def test_a_backward_rebase_shifts_the_anchor_backward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delta is signed - a shallow-buffer rebase moves the anchor the other way."""
    player = _player(monkeypatch, flow_offset_us=None)

    await _send(player, anchor_delta_us=-_ANCHOR_DELTA_US)

    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US - _ANCHOR_DELTA_US


async def test_flow_log_offset_stays_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the flow log can place the track, its anchor wins over the delta shift."""
    offset_us = 3_000_000
    player = _player(monkeypatch, flow_offset_us=offset_us)

    await _send(player, anchor_delta_us=_ANCHOR_DELTA_US)

    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US + offset_us


async def test_no_previous_anchor_falls_back_to_reported_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a schedule to shift there is nothing to derive from, so progress it is."""
    player = _player(monkeypatch, flow_offset_us=None)
    player._last_beat_queue_item_id = None
    player._last_beat_anchor_us = None

    await _send(player, anchor_delta_us=_ANCHOR_DELTA_US)

    assert player._last_beat_anchor_us == _NOW_US - _STALE_PROGRESS_MS * 1000


async def test_without_a_delta_the_anchor_still_comes_from_reported_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The contrast case: no delta means the old, queue-backed fallback.

    An ordinary refresh (the retry poller) carries no delta and has nothing better to go
    on, so it still derives the anchor from reported progress. That is exactly the stale
    input the rebase path now avoids - here it lands a full second away from the truth.
    """
    player = _player(monkeypatch, flow_offset_us=None)

    await _send(player, anchor_delta_us=0)

    assert player._last_beat_anchor_us == _NOW_US - _STALE_PROGRESS_MS * 1000
    assert player._last_beat_anchor_us != _PUBLISHED_ANCHOR_US + _ANCHOR_DELTA_US


async def test_a_second_rebase_keeps_the_delta_of_a_cancelled_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two rebases in quick succession must resolve to the sum of both deltas.

    on_flow_timeline_rebased schedules the refresh with abort_existing=True, so a second
    rebase cancels a refresh still awaiting get_audio_analysis(). The cancelled one never
    reached its publish, so _last_beat_anchor_us is still pre-rebase; carrying only the
    second delta would leave the schedule short by the first.
    """
    player = _player(monkeypatch, flow_offset_us=None)
    first_delta_us = 700_000
    second_delta_us = 900_000

    # The first refresh is cancelled mid-flight: its delta is reported but never published.
    _report_rebase(player, first_delta_us)

    await _send(player, anchor_delta_us=second_delta_us)

    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US + first_delta_us + second_delta_us
    # And the movement is consumed, so a later unrelated publish does not re-apply it.
    assert player._pending_anchor_delta_us == 0


async def test_opposing_rebases_cancel_out_and_publish_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deltas that net to zero leave the published schedule exactly where it was."""
    player = _player(monkeypatch, flow_offset_us=None)

    _report_rebase(player, _ANCHOR_DELTA_US)

    await _send(player, anchor_delta_us=-_ANCHOR_DELTA_US)

    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US


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
    player = _player(monkeypatch, flow_offset_us=None)
    harness = cast("Any", player)
    visualizer_role = cast("MagicMock", player._visualizer_role)

    async def _analysis_then_track_change(*_args: object, **_kwargs: object) -> object:
        harness._live_media = SimpleNamespace(queue_item_id="item-2")
        return SimpleNamespace(beats=[0.0, 1.0, 2.0], downbeats=[0.0])

    harness.mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=_analysis_then_track_change
    )

    await _send(player, anchor_delta_us=_ANCHOR_DELTA_US)

    # The publish path clears the role's schedule before writing the new one, so an
    # untouched role is proof this run stopped before it could overwrite item-2's.
    visualizer_role.clear_beat_schedule.assert_not_called()
    # Still naming the item that is actually published, so the next refresh for item-2
    # is not mistaken for a re-push of it.
    assert player._last_beat_queue_item_id == _QUEUE_ITEM_ID
    assert player._last_beat_anchor_us == _PUBLISHED_ANCHOR_US


async def test_a_seek_after_opposing_rebases_anchors_on_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Rebases that net to zero must not leave the rebase flagged as pending.

    Nothing is published when the deltas cancel, so the re-push guard returns early. Left
    pending, the next update for this same item - a seek - would take the fallback branch
    and re-derive the anchor already published instead of the position seeked to.
    """
    player = _player(monkeypatch, flow_offset_us=None)

    _report_rebase(player, _ANCHOR_DELTA_US)
    await _send(player, anchor_delta_us=-_ANCHOR_DELTA_US)

    seek_progress_ms = 30_000
    await player._send_beat_schedule(
        cast("object", SimpleNamespace(queue_id="q1")),  # type: ignore[arg-type]
        _queue_item(),
        seek_progress_ms,
        True,
    )

    assert player._last_beat_anchor_us == _NOW_US - seek_progress_ms * 1000
    assert player._anchor_rebase_pending is False
