"""Tests for the silence nudge that unblocks a stopped AirPlay Receiver stream."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.airplay_receiver import AirPlayReceiverProvider


def _make_daemon(reader_attaches: bool) -> MagicMock:
    """
    Build a mock receiver daemon whose audio pipe reports the given reader state.

    :param reader_attaches: Whether a consumer attaches to the audio pipe.
    """
    daemon = MagicMock()
    daemon.audio_pipe.wait_for_reader = AsyncMock(return_value=reader_attaches)
    daemon.audio_pipe.write = AsyncMock(return_value=True)
    return daemon


async def test_silence_nudge_waits_for_the_returning_reader() -> None:
    """The nudge is delivered once the stream's consumer has reattached to the pipe."""
    provider = MagicMock()
    provider.logger = MagicMock()
    daemon = _make_daemon(reader_attaches=True)

    await AirPlayReceiverProvider._write_silence_to_unblock_stream(provider, daemon)

    daemon.audio_pipe.wait_for_reader.assert_awaited_once()
    # one second of PCM_S16LE stereo 44.1kHz, enough for the consumer to emit a chunk
    assert len(daemon.audio_pipe.write.await_args.args[0]) == 176400


async def test_silence_nudge_skipped_when_no_reader_returns() -> None:
    """A pipe that nothing reads back is left alone instead of taking a dropped write."""
    provider = MagicMock()
    provider.logger = MagicMock()
    daemon = _make_daemon(reader_attaches=False)

    await AirPlayReceiverProvider._write_silence_to_unblock_stream(provider, daemon)

    daemon.audio_pipe.write.assert_not_awaited()
