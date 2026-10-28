"""Tests for the Sendspin commit loop's timeline anchor when aiosendspin moves its schedule."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.playback import (
    _HISTORY_KEEP_PAST_US,
    ANCHOR_REBASE_SIGNIFICANT_US,
    SendspinPlaybackSession,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

_SAMPLE_RATE = 48_000
_CHUNK_DURATION_US = 100_000  # matches _PRODUCER_SLICE_US so each chunk is one slice
_PCM_FORMAT = AudioFormat(sample_rate=_SAMPLE_RATE, bit_depth=32, channels=2)
_SENDSPIN_PCM_FORMAT = SendspinAudioFormat(
    sample_rate=_SAMPLE_RATE, bit_depth=32, channels=2, sample_type="float"
)
_FRAME_SIZE = 4 * 2
_CHUNK_BYTES = b"\x00" * (round(_CHUNK_DURATION_US / 1_000_000 * _SAMPLE_RATE) * _FRAME_SIZE)
_STALL_US = 3_000_000
# Audio is committed this far ahead of the clock, so now_us() and the commit timestamps
# stay on one timeline the way aiosendspin's shared RawMonotonicClock keeps them.
_SEND_AHEAD_US = 500_000


class _FakePushStream:
    """Stands in for aiosendspin's PushStream, replaying pre-scripted commit timestamps."""

    def __init__(
        self, commit_timestamps: list[int], now_timestamps: list[int] | None = None
    ) -> None:
        self._commit_timestamps = iter(commit_timestamps)
        self._now_timestamps = iter(
            now_timestamps
            if now_timestamps is not None
            else [ts - _SEND_AHEAD_US for ts in commit_timestamps]
        )
        self._now_us = commit_timestamps[0] - _SEND_AHEAD_US
        self.is_stopped = False

    def set_live_source(self, _live: bool) -> None:
        pass

    def prepare_audio(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def commit_audio(self) -> int:
        timestamp = next(self._commit_timestamps)
        self._now_us = next(self._now_timestamps)
        return timestamp

    async def sleep_to_limit_buffer(self, _limit_us: int) -> None:
        pass

    def now_us(self) -> int:
        return self._now_us

    def stop(self, *, keep_stream: bool = False) -> None:
        self.is_stopped = True

    def clear(self) -> None:
        pass


@dataclass
class _CommitLoopTrace:
    """What the commit loop did, sampled once per committed chunk."""

    anchors: list[int | None] = field(default_factory=list)
    retained: list[int] = field(default_factory=list)
    oldest_retained_start_us: list[int | None] = field(default_factory=list)
    player: MagicMock = field(default_factory=MagicMock)
    # Player-facing calls in the order the commit loop made them: ("elapsed", seconds)
    # for a published position, ("timeline", None) for a reported anchor.
    player_calls: list[tuple[str, float | None]] = field(default_factory=list)


async def _fake_audio_source(num_chunks: int) -> AsyncIterator[bytes]:
    for _ in range(num_chunks):
        yield _CHUNK_BYTES


async def _run_commit_loop(
    monkeypatch: pytest.MonkeyPatch,
    commit_timestamps: list[int],
    now_timestamps: list[int] | None = None,
) -> _CommitLoopTrace:
    """
    Drive one real _run_playback pass over the given commit schedule.

    :param commit_timestamps: Render timestamp commit_audio() returns for each chunk.
    :param now_timestamps: Clock reading after each commit. Defaults to a fixed send-ahead
        behind the commit timestamps.
    """
    player = MagicMock()
    player.player_id = "leader"
    player.mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    player.mass.config.get_raw_player_config_value.return_value = "stereo"
    player.mass.streams.get_stream.return_value = _fake_audio_source(len(commit_timestamps))
    player.api.group.stop = AsyncMock()

    session = SendspinPlaybackSession(player)
    monkeypatch.setattr(
        session, "_select_session_pcm_formats", lambda: (_PCM_FORMAT, _SENDSPIN_PCM_FORMAT)
    )
    push_stream = _FakePushStream(commit_timestamps, now_timestamps)
    monkeypatch.setattr(session, "_create_push_stream", lambda: push_stream)
    monkeypatch.setattr(
        "music_assistant.providers.sendspin.playback.import_module_in_thread", AsyncMock()
    )

    trace = _CommitLoopTrace(player=player)
    player.on_flow_timeline_changed.side_effect = lambda *_: trace.player_calls.append(
        ("timeline", None)
    )
    player.update_state.side_effect = lambda: trace.player_calls.append(
        ("elapsed", player._attr_elapsed_time)
    )
    original_prune = SendspinPlaybackSession._prune_history_locked

    def _recording_prune(self: SendspinPlaybackSession, now_monotonic_us: int) -> None:
        trace.anchors.append(self._timeline_start_us)
        original_prune(self, now_monotonic_us)
        trace.retained.append(len(self._history))
        trace.oldest_retained_start_us.append(
            self._history[0].start_time_us if self._history else None
        )

    monkeypatch.setattr(session, "_prune_history_locked", _recording_prune.__get__(session))

    media = PlayerMedia(uri="library://track/1")
    await session._run_playback(media)
    return trace


async def test_anchor_absorbs_a_mid_stream_rebase(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stall's forward rebase must shift the anchor, not just the raw timestamp."""
    t0 = 10_000_000
    commit_timestamps = [
        t0,
        t0 + _CHUNK_DURATION_US,
        # A stall pushes this commit further out than one chunk-duration would.
        t0 + 2 * _CHUNK_DURATION_US + _STALL_US,
    ]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    assert trace.anchors == [t0, t0, t0 + _STALL_US]


async def test_anchor_waits_for_the_audio_sent_before_a_rebase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor moves once the audio scheduled before a rebase has played out."""
    t0 = 10_000_000
    # The schedule moves while the first chunk still plays.
    move_now_us = t0 + 50_000
    shift_us = move_now_us + _SEND_AHEAD_US - (t0 + 2 * _CHUNK_DURATION_US)
    commit_timestamps = [
        t0,
        t0 + _CHUNK_DURATION_US,
        t0 + 2 * _CHUNK_DURATION_US + shift_us,
        t0 + 3 * _CHUNK_DURATION_US + shift_us,
    ]
    now_timestamps = [
        t0 - _SEND_AHEAD_US,
        t0 - _SEND_AHEAD_US + _CHUNK_DURATION_US,
        move_now_us,
        commit_timestamps[2] + 10_000,
    ]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps, now_timestamps)

    assert trace.anchors == [t0, t0, t0, t0 + shift_us]


async def test_a_queued_move_applies_while_the_stream_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move queued behind the playing audio is reported once it plays, with no further commit."""
    t0 = 10_000_000
    send_ahead_us = 1_000_000
    move_now_us = t0 + 50_000
    shift_us = move_now_us + send_ahead_us - (t0 + 2 * _CHUNK_DURATION_US)
    commit_timestamps = [t0, t0 + _CHUNK_DURATION_US, t0 + 2 * _CHUNK_DURATION_US + shift_us]
    now_timestamps = [t0 - send_ahead_us, t0 - send_ahead_us + _CHUNK_DURATION_US, move_now_us]

    async def _drain_until_the_move_plays(self: SendspinPlaybackSession) -> None:
        call_later = cast("MagicMock", self.player.mass.call_later)
        delay_s, apply_move = call_later.call_args.args[:2]
        push_stream = cast("_FakePushStream", self._push_stream)
        push_stream._now_us += round(delay_s * 1_000_000)
        await apply_move()

    monkeypatch.setattr(
        SendspinPlaybackSession, "_wait_for_buffer_drain", _drain_until_the_move_plays
    )

    trace = await _run_commit_loop(monkeypatch, commit_timestamps, now_timestamps)

    # The moved chunk starts 0.2 s into the flow stream.
    assert trace.player_calls[-2:] == [("elapsed", 0.2), ("timeline", None)]


async def test_first_commit_reports_the_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first commit reports its anchor, so beats published before it get retimed."""
    t0 = 10_000_000

    trace = await _run_commit_loop(monkeypatch, [t0, t0 + _CHUNK_DURATION_US])

    assert trace.player_calls == [("elapsed", 0.0), ("timeline", None)]


async def test_buffered_runahead_keeps_unplayed_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """History is pruned against what plays now, not against the commit tail."""
    t0 = 10_000_000
    num_chunks = 300
    # Contiguous audio, one chunk per commit...
    commit_timestamps = [t0 + i * _CHUNK_DURATION_US for i in range(num_chunks)]
    # ...produced ten times faster than it plays, so the buffer grows to ~27s.
    now_timestamps = [
        t0 - _SEND_AHEAD_US + i * (_CHUNK_DURATION_US // 10) for i in range(num_chunks)
    ]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps, now_timestamps)

    final_now_us = now_timestamps[-1]
    commit_tail_us = commit_timestamps[-1] + _CHUNK_DURATION_US
    assert commit_tail_us - final_now_us > 8_000_000, "fixture must actually build a buffer"

    # Everything from the playback position to the commit tail is still there.
    oldest_start_us = trace.oldest_retained_start_us[-1]
    assert oldest_start_us is not None
    assert oldest_start_us <= final_now_us
    assert trace.retained[-1] * _CHUNK_DURATION_US >= commit_tail_us - final_now_us

    # The oldest retained chunk is the one that was playing _HISTORY_KEEP_PAST_US ago.
    cutoff_us = final_now_us - _HISTORY_KEEP_PAST_US
    assert oldest_start_us <= cutoff_us < oldest_start_us + _CHUNK_DURATION_US


async def test_small_schedule_moves_add_up_to_one_rebase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Moves below the threshold report nothing alone but one rebase once they add up."""
    t0 = 5_000_000
    shift_us = ANCHOR_REBASE_SIGNIFICANT_US * 3 // 5
    commit_timestamps = [
        t0,
        t0 + _CHUNK_DURATION_US,
        t0 + 2 * _CHUNK_DURATION_US + shift_us,
        t0 + 3 * _CHUNK_DURATION_US + shift_us,
        t0 + 4 * _CHUNK_DURATION_US + 2 * shift_us,
        # Keep committing until the second move's audio plays.
        t0 + 5 * _CHUNK_DURATION_US + 2 * shift_us,
        t0 + 6 * _CHUNK_DURATION_US + 2 * shift_us,
    ]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    reported_at = [i for i, (kind, _) in enumerate(trace.player_calls) if kind == "timeline"]
    # After the first commit, only the commit where the moves add up.
    assert reported_at[1:] == [len(trace.player_calls) - 1]


async def test_rebase_publishes_its_position_step_on_the_same_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebase publishes the corrected position right away, even below the 1s gate."""
    t0 = 10_000_000
    # Enough steady commits that a position is already published before the stall.
    pre_stall = [t0 + i * _CHUNK_DURATION_US for i in range(20)]
    commit_timestamps = [*pre_stall, pre_stall[-1] + _CHUNK_DURATION_US + _STALL_US]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    kinds = [kind for kind, _ in trace.player_calls]
    # The first commit, then the stall.
    assert kinds.count("timeline") == 2

    # The beat refresh can fall back to elapsed time, so it must already be corrected.
    assert kinds[-2:] == ["elapsed", "timeline"]

    elapsed = [
        seconds for kind, seconds in trace.player_calls if kind == "elapsed" and seconds is not None
    ]
    assert len(elapsed) >= 2, "fixture must publish a position before the stall"
    # Sub-threshold step: the periodic gate alone would have withheld it.
    assert abs(elapsed[-1] - elapsed[-2]) < 1.0
