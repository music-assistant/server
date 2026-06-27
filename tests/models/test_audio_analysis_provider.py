"""Tests for the AudioAnalysisProvider base class lifecycle."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import (
    AudioAnalysisProvider,
    InstrumentedSemaphore,
)

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
async def test_start_analysis_skips_tracks_over_max_duration() -> None:
    """A provider with max_analysis_duration set rejects longer tracks before any DB/work."""
    provider = _make_provider()
    provider.max_analysis_duration = 1800.0
    provider._start_analysis = AsyncMock(return_value=True)  # type: ignore[method-assign]
    streamdetails = MagicMock()
    streamdetails.duration = 7200  # 2 hours

    accepted = await provider.start_analysis("s", streamdetails, MagicMock())

    assert accepted is False
    provider._start_analysis.assert_not_awaited()
    # The duration gate comes first, so the version lookup is skipped too.
    version_lookup = provider.mass.streams.audio_analysis.get_audio_analysis_version
    version_lookup.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_start_analysis_allows_tracks_under_max_duration() -> None:
    """Tracks within the cap proceed to the provider's own _start_analysis."""
    provider = _make_provider()
    provider.max_analysis_duration = 1800.0
    provider._start_analysis = AsyncMock(return_value=True)  # type: ignore[method-assign]
    streamdetails = MagicMock()
    streamdetails.duration = 240

    assert await provider.start_analysis("s", streamdetails, MagicMock()) is True
    provider._start_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_analysis_no_duration_cap_by_default() -> None:
    """With max_analysis_duration unset, even a very long track is accepted."""
    provider = _make_provider()
    provider._start_analysis = AsyncMock(return_value=True)  # type: ignore[method-assign]
    streamdetails = MagicMock()
    streamdetails.duration = 99999

    assert await provider.start_analysis("s", streamdetails, MagicMock()) is True


@pytest.mark.asyncio
async def test_run_offloaded_acquires_semaphore_when_present() -> None:
    """_run_offloaded holds the controller's analysis semaphore while the work runs."""
    provider = _make_provider()
    semaphore = InstrumentedSemaphore(1)
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
    semaphore = InstrumentedSemaphore(1)
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
async def test_run_offloaded_serializes_to_one_while_streaming() -> None:
    """While a player is streaming, offloads run one at a time even if the semaphore allows more."""
    provider = _make_provider()
    ctrl = provider.mass.streams.audio_analysis
    ctrl.analysis_semaphore = InstrumentedSemaphore(4)  # plenty of permits
    ctrl.analysis_solo_lock = asyncio.Lock()
    ctrl.analysis_executor = None  # fall back to asyncio.to_thread
    ctrl.playback_active = MagicMock(return_value=True)  # type: ignore[method-assign]

    first_started = threading.Event()
    first_may_finish = threading.Event()
    second_started = threading.Event()

    def _first() -> str:
        first_started.set()
        first_may_finish.wait(timeout=5)
        return "first"

    def _second() -> str:
        second_started.set()
        return "second"

    t1 = asyncio.create_task(provider._run_offloaded(_first))
    assert await asyncio.to_thread(first_started.wait, 5)

    # The second offload must block on the solo lock while the first holds it.
    t2 = asyncio.create_task(provider._run_offloaded(_second))
    await asyncio.sleep(0.1)
    assert not second_started.is_set()

    first_may_finish.set()
    assert await t1 == "first"
    assert await t2 == "second"
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_run_offloaded_runs_concurrently_when_idle() -> None:
    """With no player streaming, the solo lock is not taken and offloads overlap up to the cap."""
    provider = _make_provider()
    ctrl = provider.mass.streams.audio_analysis
    ctrl.analysis_semaphore = InstrumentedSemaphore(4)
    ctrl.analysis_solo_lock = asyncio.Lock()
    ctrl.analysis_executor = None
    ctrl.playback_active = MagicMock(return_value=False)  # type: ignore[method-assign]

    first_started = threading.Event()
    first_may_finish = threading.Event()
    second_started = threading.Event()

    def _first() -> str:
        first_started.set()
        first_may_finish.wait(timeout=5)
        return "first"

    def _second() -> str:
        second_started.set()
        return "second"

    t1 = asyncio.create_task(provider._run_offloaded(_first))
    assert await asyncio.to_thread(first_started.wait, 5)
    t2 = asyncio.create_task(provider._run_offloaded(_second))
    # Idle: the second offload runs without waiting for the first to finish.
    assert await asyncio.to_thread(second_started.wait, 5)

    first_may_finish.set()
    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_run_offloaded_uses_dedicated_executor_when_present() -> None:
    """Offloads run on the controller's dedicated (niced) analysis pool when configured."""
    provider = _make_provider()
    ctrl = provider.mass.streams.audio_analysis
    ctrl.analysis_semaphore = InstrumentedSemaphore(1)
    ctrl.analysis_solo_lock = None  # not an asyncio.Lock -> solo step skipped
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
    ctrl.analysis_executor = executor
    try:
        thread_name = await provider._run_offloaded(lambda: threading.current_thread().name)
    finally:
        executor.shutdown(wait=False)
    assert thread_name.startswith("analysis")


@pytest.mark.asyncio
async def test_run_offloaded_releases_permit_if_scheduling_fails() -> None:
    """A failure to schedule the worker must release the permit, not leak it."""
    provider = _make_provider()
    semaphore = InstrumentedSemaphore(1)
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


@pytest.mark.asyncio
async def test_ensure_models_loaded_loads_once() -> None:
    """Concurrent ensure_models_loaded calls load the heavy models a single time."""
    provider = _make_provider()
    provider.has_unloadable_models = True
    calls = 0

    async def _load() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)

    provider._load_models = _load  # type: ignore[method-assign]

    results = await asyncio.gather(*(provider.ensure_models_loaded() for _ in range(5)))

    assert all(results)
    assert calls == 1
    assert provider._models_loaded is True


@pytest.mark.asyncio
async def test_unload_idle_models_frees_then_reloads() -> None:
    """unload_idle_models frees the models; the next ensure reloads them."""
    provider = _make_provider()
    provider.has_unloadable_models = True
    load_calls = 0
    free_calls = 0

    async def _load() -> None:
        nonlocal load_calls
        load_calls += 1

    def _free() -> None:
        nonlocal free_calls
        free_calls += 1

    provider._load_models = _load  # type: ignore[method-assign]
    provider._free_models = _free  # type: ignore[method-assign]

    await provider.ensure_models_loaded()
    await provider.unload_idle_models()
    assert free_calls == 1
    assert provider._models_loaded is False

    await provider.ensure_models_loaded()
    assert load_calls == 2  # reloaded on demand
    assert provider._models_loaded is True


@pytest.mark.asyncio
async def test_start_analysis_rejects_when_model_load_fails() -> None:
    """A provider whose model load fails declines the session instead of starting it."""
    provider = _make_provider()
    provider.has_unloadable_models = True
    provider._start_analysis = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _load() -> None:
        raise RuntimeError("model load boom")

    provider._load_models = _load  # type: ignore[method-assign]
    streamdetails = MagicMock()
    streamdetails.duration = 120

    assert await provider.start_analysis("s", streamdetails, MagicMock()) is False
    provider._start_analysis.assert_not_awaited()
    assert provider._models_loaded is False
