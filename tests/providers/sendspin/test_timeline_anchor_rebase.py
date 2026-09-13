"""
Tests for the Sendspin commit loop's timeline anchor across a producer stall.

aiosendspin rebases its scheduled play timeline forward whenever audio
production stalls (server/push_stream.py, _resolve_channel_play_start:
"If audio production stalls ... rebase the timeline so new audio is always
scheduled at least _min_send_ahead_us() from 'now'"). Before this fix,
SendspinPlaybackSession._timeline_start_us was captured once from the first
committed chunk and never updated, so every position derived from it
(elapsed_time, the beat-schedule anchor, join-catchup history pruning)
silently overstated how far into the track playback actually was, by the
stall duration, for the rest of the stream.

Reproduces the stall by driving the real commit loop (_run_playback) with a
fake push stream whose commit_audio() jumps forward mid-stream, exactly as
aiosendspin's rebase would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession

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


class _FakePushStream:
    """Stands in for aiosendspin's PushStream, replaying pre-scripted commit timestamps."""

    def __init__(self, commit_timestamps: list[int]) -> None:
        self._commit_timestamps = iter(commit_timestamps)
        self._now_us = 0
        self.is_stopped = False

    def set_live_source(self, _live: bool) -> None:
        pass

    def prepare_audio(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def commit_audio(self) -> int:
        return next(self._commit_timestamps)

    async def sleep_to_limit_buffer(self, _limit_us: int) -> None:
        pass

    def now_us(self) -> int:
        self._now_us += 1_000
        return self._now_us

    def stop(self, *, keep_stream: bool = False) -> None:
        self.is_stopped = True

    def clear(self) -> None:
        pass


async def _fake_audio_source(num_chunks: int) -> AsyncIterator[bytes]:
    for _ in range(num_chunks):
        yield _CHUNK_BYTES


async def _run_commit_loop(
    monkeypatch: pytest.MonkeyPatch, commit_timestamps: list[int]
) -> list[int | None]:
    """Drive one real _run_playback pass and return _timeline_start_us after each commit."""
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
    push_stream = _FakePushStream(commit_timestamps)
    monkeypatch.setattr(session, "_create_push_stream", lambda: push_stream)
    monkeypatch.setattr(
        "music_assistant.providers.sendspin.playback.import_module_in_thread", AsyncMock()
    )

    observed_anchors: list[int | None] = []
    original_prune = SendspinPlaybackSession._prune_history_locked

    def _recording_prune(self: SendspinPlaybackSession, now_monotonic_us: int) -> None:
        observed_anchors.append(self._timeline_start_us)
        original_prune(self, now_monotonic_us)

    monkeypatch.setattr(session, "_prune_history_locked", _recording_prune.__get__(session))

    media = PlayerMedia(uri="library://track/1")
    await session._run_playback(media)
    return observed_anchors


async def test_anchor_absorbs_a_mid_stream_rebase(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stall's forward rebase must shift the anchor, not just the raw timestamp."""
    t0 = 10_000_000
    commit_timestamps = [
        t0,
        t0 + _CHUNK_DURATION_US,
        # A stall pushes this commit further out than one chunk-duration would.
        t0 + 2 * _CHUNK_DURATION_US + _STALL_US,
    ]

    anchors = await _run_commit_loop(monkeypatch, commit_timestamps)

    assert anchors == [t0, t0, t0 + _STALL_US]


async def test_anchor_is_stable_with_no_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a steady schedule the anchor must stay put across every commit."""
    t0 = 5_000_000
    commit_timestamps = [t0 + i * _CHUNK_DURATION_US for i in range(4)]

    anchors = await _run_commit_loop(monkeypatch, commit_timestamps)

    assert anchors == [t0] * 4
