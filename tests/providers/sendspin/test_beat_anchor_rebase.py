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
from typing import TYPE_CHECKING, cast
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
    monkeypatch.setattr(
        SendspinPlayer, "_visualizer_role", property(lambda _self: MagicMock()), raising=False
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
    # The flow log is what decides whether the preferred path is available at all.
    monkeypatch.setattr(
        SendspinPlayer, "_flow_track_offset_us", staticmethod(lambda _d, _i: flow_offset_us)
    )
    return player


async def _send(player: SendspinPlayer, *, anchor_delta_us: int) -> None:
    await player._send_beat_schedule(
        cast("object", SimpleNamespace(queue_id="q1")),  # type: ignore[arg-type]
        _queue_item(),
        _STALE_PROGRESS_MS,
        True,
        anchor_delta_us=anchor_delta_us,
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
