"""
Tests for the Sendspin commit loop's timeline anchor across a producer stall.

aiosendspin rebases its scheduled play timeline forward whenever audio
production stalls (server/push_stream.py, _resolve_channel_play_start:
"If audio production stalls ... rebase the timeline so new audio is always
scheduled at least _min_send_ahead_us() from 'now'"). Before this fix,
SendspinPlaybackSession._timeline_start_us was captured once from the first
committed chunk and never updated, so every position derived from it
(elapsed_time and the beat-schedule anchor) silently overstated how far into
the track playback actually was, by the stall duration, for the rest of the
stream.

Reproduces the stall by driving the real commit loop (_run_playback) with a
fake push stream whose commit_audio() jumps forward mid-stream, exactly as
aiosendspin's rebase would.

History pruning is covered here too, because it is the other thing the commit
loop does per chunk and the anchor is the wrong clock for it: the producer runs
ahead of the speakers, so pruning follows the push stream's clock instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING
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
_FRAME_SIZE = 4 * 2  # 32-bit float * 2 channels
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
        # Default: audio is committed a fixed distance ahead of the clock, so now_us() and
        # the commit timestamps stay on one timeline the way aiosendspin's shared
        # RawMonotonicClock keeps them. A caller can script the clock separately to model a
        # producer running faster than realtime and building a buffer.
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
    # for a published position, ("rebased", None) for a reported anchor rebase.
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
    player.on_flow_timeline_rebased.side_effect = lambda *_: trace.player_calls.append(
        ("rebased", None)
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


async def test_stall_does_not_prune_the_chunk_it_just_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A stall must not empty the history join-catchup backfills from.

    _prune_history_locked pairs the anchor with a monotonic reference, and a stall moves
    both. Rebasing only the anchor counts the stall twice and pushes the cutoff past the
    chunk that was just committed, leaving a late joiner nothing at all to catch up from.
    Chunks genuinely older than _HISTORY_KEEP_PAST_US are still dropped, as on a steady
    schedule - the newest one must survive.
    """
    t0 = 10_000_000
    commit_timestamps = [
        t0,
        t0 + _CHUNK_DURATION_US,
        t0 + 2 * _CHUNK_DURATION_US + _STALL_US,
    ]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    assert trace.retained == [1, 2, 1]


async def test_anchor_is_stable_with_no_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a steady schedule the anchor must stay put across every commit."""
    t0 = 5_000_000
    commit_timestamps = [t0 + i * _CHUNK_DURATION_US for i in range(4)]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    assert trace.anchors == [t0] * 4
    assert trace.retained == [1, 2, 3, 4]


async def test_buffered_runahead_keeps_unplayed_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    History must be pruned against the speakers, not against the commit tail.

    The producer runs faster than realtime until sleep_to_limit_buffer() throttles it at
    _PRODUCER_BUFFER_LIMIT_US, so the newest committed chunk is scheduled to render many
    seconds from now. All of that committed-but-unplayed audio is exactly what a late
    joiner is backfilled from, so only what has already left the speakers may be dropped.
    """
    t0 = 10_000_000
    num_chunks = 100
    # Contiguous audio, one chunk per commit...
    commit_timestamps = [t0 + i * _CHUNK_DURATION_US for i in range(num_chunks)]
    # ...produced ten times faster than it plays, so the buffer grows to ~9s.
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

    # Chunks that finished rendering more than _HISTORY_KEEP_PAST_US ago are still dropped.
    assert oldest_start_us >= final_now_us - _HISTORY_KEEP_PAST_US - _CHUNK_DURATION_US


async def test_rebase_is_reported_to_the_player(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A rebase must be reported so the player can re-publish its beat schedule.

    Beat timings go out as absolute server-clock timestamps derived from the anchor, and
    nothing else re-publishes them mid-track: the media-updated callback fires on media
    identity changes, not on elapsed-time updates.
    """
    t0 = 10_000_000
    commit_timestamps = [
        t0,
        t0 + _CHUNK_DURATION_US,
        t0 + 2 * _CHUNK_DURATION_US + _STALL_US,
    ]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    trace.player.on_flow_timeline_rebased.assert_called_once()


async def test_no_rebase_is_reported_on_a_steady_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commit-to-commit jitter below the rebase threshold must not re-publish anything."""
    t0 = 5_000_000
    jitter_us = ANCHOR_REBASE_SIGNIFICANT_US // 10
    commit_timestamps = [t0 + i * _CHUNK_DURATION_US + (i % 2) * jitter_us for i in range(4)]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    trace.player.on_flow_timeline_rebased.assert_not_called()


async def test_rebase_publishes_its_position_step_on_the_same_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A rebase must publish the corrected position on the commit that rebased.

    The step a rebase puts in the reported position is (buffer_depth + chunk -
    min_send_ahead) and has nothing to do with how long the stall lasted, so it can be
    well under the periodic gate's 1s threshold. The gate would then hold the correction
    back until ordinary playback drifted a full second past the last published value,
    which is the overshoot this PR exists to remove.

    The fixture keeps a constant send-ahead, so the step here is one chunk - far below
    the gate - and only the explicit publish can get it out.
    """
    t0 = 10_000_000
    # Enough steady commits that a position is already published before the stall.
    pre_stall = [t0 + i * _CHUNK_DURATION_US for i in range(20)]
    commit_timestamps = [*pre_stall, pre_stall[-1] + _CHUNK_DURATION_US + _STALL_US]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    kinds = [kind for kind, _ in trace.player_calls]
    assert kinds.count("rebased") == 1

    # The rebasing commit published a position, and did so before reporting the rebase:
    # the beat schedule falls back to elapsed time when the flow log has not recorded
    # the current track yet.
    assert kinds[-2:] == ["elapsed", "rebased"]

    elapsed = [
        seconds for kind, seconds in trace.player_calls if kind == "elapsed" and seconds is not None
    ]
    assert len(elapsed) >= 2, "fixture must publish a position before the stall"
    # Sub-threshold step: the periodic gate alone would have withheld it.
    assert abs(elapsed[-1] - elapsed[-2]) < 1.0


async def test_steady_commits_publish_on_the_periodic_gate_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a rebase, positions go out on the 1s gate and never more often."""
    t0 = 10_000_000
    commit_timestamps = [t0 + i * _CHUNK_DURATION_US for i in range(40)]

    trace = await _run_commit_loop(monkeypatch, commit_timestamps)

    assert "rebased" not in [kind for kind, _ in trace.player_calls]
    elapsed = [
        seconds for kind, seconds in trace.player_calls if kind == "elapsed" and seconds is not None
    ]
    assert all(b - a >= 1.0 for a, b in pairwise(elapsed))
