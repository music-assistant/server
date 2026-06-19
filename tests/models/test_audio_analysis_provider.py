"""Tests for the AudioAnalysisProvider base class lifecycle."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails


class _StubProvider(AudioAnalysisProvider):
    """Minimal concrete provider for base-class tests."""

    async def _start_analysis(
        self, session_id: str, streamdetails: StreamDetails, audio_format: AudioFormat
    ) -> bool:
        return True

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        return None

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        return None


def _make_provider() -> _StubProvider:
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    manifest = MagicMock()
    manifest.domain = "test_stub_provider"
    config = MagicMock()
    config.get_value = MagicMock(return_value="GLOBAL")
    return _StubProvider(mass, manifest, config, supported_features=set())


@pytest.mark.asyncio
async def test_post_analysis_default_is_noop() -> None:
    """Default post_analysis must be a no-op that returns None."""
    provider = _make_provider()
    streamdetails = MagicMock()
    analysis = AudioAnalysisData()
    await provider.post_analysis(streamdetails, analysis)


@pytest.mark.asyncio
async def test_finalize_calls_post_analysis_when_finalize_returns_analysis() -> None:
    """When _finalize returns analysis, finalize must call post_analysis with it."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()
    analysis = AudioAnalysisData(loudness_integrated=-14.0)

    provider._finalize = AsyncMock(return_value=analysis)  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await provider.start_analysis("session-1", streamdetails, audio_format)
    await provider.finalize("session-1")

    provider.post_analysis.assert_awaited_once_with(streamdetails, analysis)
    assert "session-1" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_skips_post_analysis_when_finalize_returns_none() -> None:
    """When _finalize returns None, post_analysis must NOT be called."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()

    provider._finalize = AsyncMock(return_value=None)  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await provider.start_analysis("session-2", streamdetails, audio_format)
    await provider.finalize("session-2")

    provider.post_analysis.assert_not_awaited()
    assert "session-2" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_swallows_finalize_exception_and_skips_post_analysis() -> None:
    """If _finalize raises, post_analysis must not be called and the exception must not propagate."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()

    provider._finalize = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await provider.start_analysis("session-3", streamdetails, audio_format)
    await provider.finalize("session-3")

    provider.post_analysis.assert_not_awaited()
    assert "session-3" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_swallows_post_analysis_exception() -> None:
    """post_analysis raising must be caught; the analysis row stays valid."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()
    analysis = AudioAnalysisData()

    provider._finalize = AsyncMock(return_value=analysis)  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(side_effect=RuntimeError("tag write failed"))  # type: ignore[method-assign]

    await provider.start_analysis("session-4", streamdetails, audio_format)
    # Must not raise
    await provider.finalize("session-4")

    provider.post_analysis.assert_awaited_once()
    assert "session-4" not in provider._sessions


@pytest.mark.asyncio
async def test_run_offloaded_acquires_semaphore_when_present() -> None:
    """_run_offloaded holds the controller's analysis semaphore while the work runs."""
    provider = _make_provider()
    semaphore = asyncio.Semaphore(1)
    provider.mass.streams.audio_analysis.analysis_semaphore = semaphore

    def _work() -> bool:
        # Runs in the worker thread; the cap must already be held here.
        return semaphore.locked()

    held_during = await provider._run_offloaded(_work)
    assert held_during is True
    assert not semaphore.locked()


@pytest.mark.asyncio
async def test_run_offloaded_without_cap_runs_plainly() -> None:
    """With no real semaphore configured, _run_offloaded still runs the work and forwards args."""
    provider = _make_provider()
    # The MagicMock attribute is not an asyncio.Semaphore, so this falls back to a plain thread.
    result = await provider._run_offloaded(lambda value: value * 2, 21)
    assert result == 42


@pytest.mark.asyncio
async def test_run_offloaded_holds_permit_until_thread_finishes_on_cancel() -> None:
    """Cancelling the awaiter must not free the permit while the worker thread is still running."""
    provider = _make_provider()
    semaphore = asyncio.Semaphore(1)
    provider.mass.streams.audio_analysis.analysis_semaphore = semaphore

    started = threading.Event()
    may_finish = threading.Event()

    def _blocking() -> str:
        started.set()
        may_finish.wait(timeout=5)
        return "done"

    task = asyncio.create_task(provider._run_offloaded(_blocking))
    assert await asyncio.to_thread(started.wait, 5)  # thread is running, permit acquired
    assert semaphore.locked()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Awaiter cancelled, but the thread is still running — the permit must NOT be freed yet.
    assert semaphore.locked()

    may_finish.set()  # let the thread complete; the done-callback then releases the permit
    for _ in range(100):
        if not semaphore.locked():
            break
        await asyncio.sleep(0.02)
    assert not semaphore.locked()


@pytest.mark.asyncio
async def test_run_offloaded_releases_permit_if_scheduling_fails() -> None:
    """A failure to schedule the worker must release the permit, not leak it."""
    provider = _make_provider()
    semaphore = asyncio.Semaphore(1)
    provider.mass.streams.audio_analysis.analysis_semaphore = semaphore

    with (
        patch(
            "music_assistant.models.audio_analysis_provider.asyncio.to_thread",
            side_effect=RuntimeError("cannot schedule"),
        ),
        pytest.raises(RuntimeError, match="cannot schedule"),
    ):
        await provider._run_offloaded(lambda: "x")

    assert not semaphore.locked()  # permit released despite the failure
