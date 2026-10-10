"""
Tests for the elapsed-time anchor a short playback session publishes.

A session whose audio all fits in the producer buffer commits in one burst and
ends before the stream clock is a second past the first chunk.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.providers.sendspin.playback import SendspinPlaybackSession

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32, channels=2
)
FIRST_CHUNK_START_US = 1_000_000
# The stream clock stays short of the first chunk's start for the whole session.
CLOCK_NOW_US = 900_000


def _short_session(monkeypatch: pytest.MonkeyPatch) -> SendspinPlaybackSession:
    """Create a session whose whole source fits in the producer buffer."""
    session = object.__new__(SendspinPlaybackSession)
    session.player = MagicMock()
    session.player.api.group.clients = []
    session.player._attr_elapsed_time = None
    session.player._attr_elapsed_time_last_updated = None
    session._state_lock = asyncio.Lock()
    session._pipeline_config_cache = {}
    session._push_stream = None
    session._history = deque()
    session._produced_audio_us = 0
    session._timeline_start_us = None
    session._first_commit_monotonic_us = None
    session._cancel_requested = False

    async def audio_source() -> AsyncIterator[bytes]:
        # 2s of silence: well inside the 30s producer buffer
        yield bytes(PCM_FORMAT.sample_rate * PCM_FORMAT.channels * 4 * 2)

    session.player.mass.streams.get_stream = MagicMock(return_value=audio_source())
    push_stream = MagicMock()
    push_stream.is_stopped = False
    push_stream.commit_audio = AsyncMock(return_value=FIRST_CHUNK_START_US)
    push_stream.sleep_to_limit_buffer = AsyncMock()
    push_stream.now_us = MagicMock(return_value=CLOCK_NOW_US)

    monkeypatch.setattr(session, "_get_start_streamdetails", MagicMock(return_value=None))
    monkeypatch.setattr(session, "_follow_session_sample_rate", AsyncMock())
    monkeypatch.setattr(
        session, "_select_session_pcm_formats", MagicMock(return_value=(PCM_FORMAT, MagicMock()))
    )
    monkeypatch.setattr(session, "_is_live_source", MagicMock(return_value=False))
    monkeypatch.setattr(session, "_create_push_stream", MagicMock(return_value=push_stream))
    monkeypatch.setattr(session, "_refresh_member_mappings", AsyncMock())
    monkeypatch.setattr(session, "_snapshot_active_pipelines", AsyncMock(return_value=(set(), [])))
    monkeypatch.setattr(session, "_inject_ready_join_historical", AsyncMock(return_value=False))
    monkeypatch.setattr(session, "_fanout_history_chunk_to_join_processors", AsyncMock())
    monkeypatch.setattr(session, "_attach_task_exception_logger", MagicMock())
    monkeypatch.setattr(session, "_wait_for_buffer_drain", AsyncMock())
    monkeypatch.setattr(session, "_stop_push_stream", MagicMock())
    monkeypatch.setattr(session, "_clear_join_catchup", AsyncMock())
    monkeypatch.setattr(session, "_clear_member_pipelines", AsyncMock())
    monkeypatch.setattr(session, "_reset_session_state", AsyncMock())
    return session


async def test_session_within_buffer_publishes_elapsed_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first commit anchors elapsed time so the queue can extrapolate from it."""
    session = _short_session(monkeypatch)

    await session._run_playback(MagicMock())

    assert session.player._attr_elapsed_time == 0.0
    assert session.player._attr_elapsed_time_last_updated is not None
    cast("MagicMock", session.player.update_state).assert_called()
