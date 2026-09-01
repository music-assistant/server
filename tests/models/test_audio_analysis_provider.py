"""Tests for the AudioAnalysisProvider base class lifecycle."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.models.audio_analysis_provider import (
    AudioAnalysisProvider,
    InstrumentedSemaphore,
)
from tests.common import collect_loop_errors

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
    mass.streams.audio_analysis.record_analysis_failure = AsyncMock()
    mass.streams.audio_analysis.clear_analysis_failure = AsyncMock()
    manifest = MagicMock()
    manifest.domain = "test_stub_provider"
    config = MagicMock()
    config.get_value = MagicMock(return_value="GLOBAL")
    return _StubProvider(mass, manifest, config, supported_features=set())


@pytest.mark.asyncio
async def test_abort_records_a_blocking_failure_and_drops_the_session() -> None:
    """abort() records the failure with no retry time so the track stays skipped."""
    provider = _make_provider()
    session = MagicMock()
    provider._sessions["sess"] = session
    provider._record_failure = AsyncMock()  # type: ignore[method-assign]

    await provider.abort("sess", "audio processing failed (boom)")

    assert "sess" not in provider._sessions
    provider._record_failure.assert_awaited_once_with(
        session, "audio processing failed (boom)", None
    )


@pytest.mark.asyncio
async def test_abort_on_unknown_session_records_nothing() -> None:
    """Aborting a session that already ended must not record a failure."""
    provider = _make_provider()
    provider._record_failure = AsyncMock()  # type: ignore[method-assign]

    await provider.abort("missing", "audio processing failed (boom)")

    provider._record_failure.assert_not_awaited()


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
async def test_run_offloaded_timed_returns_result_and_execution_seconds() -> None:
    """_run_offloaded_timed forwards args and reports the callable's own execution time."""
    provider = _make_provider()

    def _work(value: int) -> int:
        time.sleep(0.05)
        return value * 2

    result, seconds = await provider._run_offloaded_timed(_work, 21)
    assert result == 42
    assert seconds >= 0.05


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
async def test_run_offloaded_worker_failure_after_cancel_logs_no_loop_error() -> None:
    """A worker failing after the awaiter was cancelled must not be reported to the loop."""
    provider = _make_provider()
    semaphore = InstrumentedSemaphore(1)
    provider.mass.streams.audio_analysis.analysis_semaphore = semaphore

    started = threading.Event()
    may_finish = threading.Event()

    def _failing() -> str:
        started.set()
        may_finish.wait(timeout=5)
        raise RuntimeError("boom")

    with collect_loop_errors() as reported:
        task = asyncio.create_task(provider._run_offloaded(_failing))
        assert await asyncio.to_thread(started.wait, 5)  # thread is running, permit acquired
        assert semaphore.locked()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # release the worker only once the cancellation is fully processed, so the failure
        # reliably lands after the awaiter has already given up -- releasing earlier would
        # let the failure race the cancellation and pass even on the buggy shield-based code
        may_finish.set()

        # the done-callback releases the permit once the worker (and its failure) is
        # observed, so waiting for that also gives any (buggy) loop report time to land
        for _ in range(100):
            if not semaphore.locked():
                break
            await asyncio.sleep(0.02)
        assert not semaphore.locked()  # permit still released despite the failure

    assert reported == []


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


def test_audio_analysis_error_carries_reason_and_retry() -> None:
    """AudioAnalysisError exposes reason and retry_at; retry_at defaults to None."""
    err = AudioAnalysisError("bad file")
    assert err.reason == "bad file"
    assert err.retry_at is None

    when = datetime(2030, 1, 1, tzinfo=UTC)
    err2 = AudioAnalysisError("offline", retry_at=when)
    assert err2.retry_at == when
    assert str(err2) == "offline"


def test_audio_analysis_error_rejects_naive_retry_at() -> None:
    """A naive (tz-unaware) retry_at is rejected to avoid silent epoch skew."""
    with pytest.raises(ValueError, match="timezone-aware"):
        AudioAnalysisError("x", retry_at=datetime(2030, 1, 1))  # noqa: DTZ001


@pytest.mark.asyncio
async def test_finalize_records_classified_failure_on_audio_analysis_error() -> None:
    """A raised AudioAnalysisError in _finalize records reason + retry_at and skips persist."""
    provider = _make_provider()
    streamdetails = MagicMock()
    streamdetails.item_id = "track-1"
    streamdetails.provider = "test_prov"
    streamdetails.media_type = "track"
    when = datetime(2030, 1, 1, tzinfo=UTC)

    provider._finalize = AsyncMock(  # type: ignore[method-assign]
        side_effect=AudioAnalysisError("no usable audio frames extracted", retry_at=when)
    )

    await provider.start_analysis("s1", streamdetails, MagicMock())
    await provider.finalize("s1")

    rec = cast("AsyncMock", provider.mass.streams.audio_analysis.record_analysis_failure)
    rec.assert_awaited_once()
    kwargs = rec.call_args.kwargs
    assert kwargs["reason"] == "no usable audio frames extracted"
    assert kwargs["retry_at"] == when
    assert kwargs["aa_provider_domain"] == "test_stub_provider"
    cast("AsyncMock", provider.mass.streams.audio_analysis.set_audio_analysis).assert_not_awaited()
    assert "s1" not in provider._sessions


@pytest.mark.asyncio
async def test_record_failure_passes_provider_analysis_version() -> None:
    """Failures are recorded at the provider's analysis_version so a version bump unblocks them."""
    provider = _make_provider()
    provider.analysis_version = 7
    streamdetails = MagicMock()
    streamdetails.item_id = "track-3"
    streamdetails.provider = "test_prov"
    streamdetails.media_type = "track"

    provider._finalize = AsyncMock(side_effect=AudioAnalysisError("boom"))  # type: ignore[method-assign]

    await provider.start_analysis("s5", streamdetails, MagicMock())
    await provider.finalize("s5")

    rec = cast("AsyncMock", provider.mass.streams.audio_analysis.record_analysis_failure)
    rec.assert_awaited_once()
    assert rec.call_args.kwargs["analysis_version"] == 7


@pytest.mark.asyncio
async def test_finalize_records_never_retry_on_generic_exception() -> None:
    """A generic exception in _finalize records str(err) with retry_at None."""
    provider = _make_provider()
    streamdetails = MagicMock()
    streamdetails.item_id = "track-2"
    streamdetails.provider = "test_prov"
    streamdetails.media_type = "track"

    provider._finalize = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    await provider.start_analysis("s2", streamdetails, MagicMock())
    await provider.finalize("s2")

    rec = cast("AsyncMock", provider.mass.streams.audio_analysis.record_analysis_failure)
    rec.assert_awaited_once()
    assert rec.call_args.kwargs["reason"] == "boom"
    assert rec.call_args.kwargs["retry_at"] is None
    assert "s2" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_no_record_on_none_return() -> None:
    """A plain None return from _finalize records nothing (deliberate skip)."""
    provider = _make_provider()
    streamdetails = MagicMock()
    provider._finalize = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await provider.start_analysis("s3", streamdetails, MagicMock())
    await provider.finalize("s3")

    cast(
        "AsyncMock", provider.mass.streams.audio_analysis.record_analysis_failure
    ).assert_not_awaited()


@pytest.mark.asyncio
async def test_start_analysis_records_on_audio_analysis_error() -> None:
    """A raised AudioAnalysisError in _start_analysis records the failure and rejects."""
    provider = _make_provider()
    streamdetails = MagicMock()
    streamdetails.item_id = "track-4"
    streamdetails.provider = "test_prov"
    streamdetails.media_type = "track"

    provider._start_analysis = AsyncMock(  # type: ignore[method-assign]
        side_effect=AudioAnalysisError("unsupported codec")
    )

    accepted = await provider.start_analysis("s4", streamdetails, MagicMock())

    assert accepted is False
    rec = cast("AsyncMock", provider.mass.streams.audio_analysis.record_analysis_failure)
    rec.assert_awaited_once()
    assert rec.call_args.kwargs["reason"] == "unsupported codec"
    assert "s4" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_swallows_recorder_error() -> None:
    """If record_analysis_failure raises, finalize must not propagate and must still clean up."""
    provider = _make_provider()
    provider.logger = MagicMock()
    streamdetails = MagicMock()
    streamdetails.item_id = "track-x"
    streamdetails.provider = "test_prov"
    streamdetails.media_type = "track"
    provider._finalize = AsyncMock(  # type: ignore[method-assign]
        side_effect=AudioAnalysisError("boom")
    )
    aa = cast("MagicMock", provider.mass.streams.audio_analysis)
    aa.record_analysis_failure = AsyncMock(side_effect=sqlite3.OperationalError("db down"))

    await provider.start_analysis("sx", streamdetails, MagicMock())
    # Must not raise despite the recorder failing.
    await provider.finalize("sx")

    provider.logger.warning.assert_called()
    assert "sx" not in provider._sessions


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


@pytest.mark.asyncio
async def test_unload_cancels_in_flight_finalize_before_freeing_models() -> None:
    """unload() cancels a running finalize and frees the models only once it has unwound."""
    provider = _make_provider()
    provider.has_unloadable_models = True
    started = asyncio.Event()
    running = False
    cancelled = False
    freed_while_running = False

    async def _finalize(session_id: str) -> AudioAnalysisData | None:  # noqa: ARG001
        nonlocal running, cancelled
        running = True
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            running = False
        return None

    def _free() -> None:
        nonlocal freed_while_running
        if running:
            freed_while_running = True

    provider._load_models = AsyncMock()  # type: ignore[method-assign]
    provider._free_models = _free  # type: ignore[method-assign]
    provider._finalize = _finalize  # type: ignore[method-assign]

    await provider.ensure_models_loaded()
    task = asyncio.create_task(provider.finalize("s-inflight"))
    await started.wait()
    assert provider._finalize_tasks == {task}

    await asyncio.wait_for(provider.unload(), timeout=5)

    assert cancelled is True
    assert freed_while_running is False
    assert task.cancelled()
    assert not provider._finalize_tasks
    assert provider._models_loaded is False
    recorder = cast("AsyncMock", provider.mass.streams.audio_analysis.record_analysis_failure)
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_unload_frees_models_when_no_finalize_in_flight() -> None:
    """unload() with nothing in flight frees the models as before."""
    provider = _make_provider()
    provider.has_unloadable_models = True
    free_calls = 0

    def _free() -> None:
        nonlocal free_calls
        free_calls += 1

    provider._load_models = AsyncMock()  # type: ignore[method-assign]
    provider._free_models = _free  # type: ignore[method-assign]

    await provider.ensure_models_loaded()
    assert provider._models_loaded is True

    await asyncio.wait_for(provider.unload(), timeout=5)

    assert free_calls == 1
    assert provider._models_loaded is False


@pytest.mark.asyncio
async def test_unload_from_within_finalize_does_not_cancel_itself() -> None:
    """A finalize that reaches unload() must not cancel the task it is running on."""
    provider = _make_provider()
    provider.has_unloadable_models = True

    async def _finalize(session_id: str) -> AudioAnalysisData | None:  # noqa: ARG001
        await provider.unload()
        return None

    provider._load_models = AsyncMock()  # type: ignore[method-assign]
    provider._free_models = MagicMock()  # type: ignore[method-assign]
    provider._finalize = _finalize  # type: ignore[method-assign]

    await provider.ensure_models_loaded()
    await asyncio.wait_for(provider.finalize("s-self"), timeout=5)

    assert provider._models_loaded is False
    assert not provider._finalize_tasks


@pytest.mark.asyncio
async def test_finalize_is_skipped_while_unloading() -> None:
    """A finalize issued while unloading runs no inference, registers nothing, clears its session."""
    provider = _make_provider()
    provider._finalize = AsyncMock(return_value=None)  # type: ignore[method-assign]
    provider._sessions["s-gate"] = MagicMock()
    provider.unloading = True

    await provider.finalize("s-gate")

    provider._finalize.assert_not_awaited()
    assert not provider._finalize_tasks
    assert "s-gate" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_started_during_unload_does_not_run_inference() -> None:
    """A finalize registered while unload() unwinds must not infer against models being freed."""
    provider = _make_provider()
    provider.has_unloadable_models = True
    finalized: list[str] = []
    late_task: asyncio.Task[None] | None = None
    started = asyncio.Event()

    async def _finalize(session_id: str) -> AudioAnalysisData | None:
        nonlocal late_task
        finalized.append(session_id)
        if session_id != "s-first":
            return None
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # mirrors the controller resolving the provider and finalizing another session
            # while unload() is suspended awaiting this cancellation
            late_task = asyncio.create_task(provider.finalize("s-late"))
            raise
        return None

    provider._load_models = AsyncMock()  # type: ignore[method-assign]
    provider._free_models = MagicMock()  # type: ignore[method-assign]
    provider._finalize = _finalize  # type: ignore[method-assign]

    await provider.ensure_models_loaded()
    first = asyncio.create_task(provider.finalize("s-first"))
    await started.wait()

    # mass.unload_provider sets this before it awaits provider.unload()
    provider.unloading = True
    await asyncio.wait_for(provider.unload(), timeout=5)

    assert late_task is not None
    await asyncio.wait_for(late_task, timeout=5)
    assert finalized == ["s-first"]
    assert first.cancelled()
    assert not provider._finalize_tasks


@pytest.mark.asyncio
async def test_start_analysis_declines_while_unloading() -> None:
    """A session arriving from a stale provider snapshot must not start on a dying provider."""
    provider = _make_provider()
    provider._start_analysis = AsyncMock(return_value=True)  # type: ignore[method-assign]
    provider.unloading = True

    accepted = await provider.start_analysis("s", MagicMock(), MagicMock())

    assert accepted is False
    provider._start_analysis.assert_not_awaited()
    # The gate comes first, so nothing downstream of it runs either.
    version_lookup = provider.mass.streams.audio_analysis.get_audio_analysis_version
    version_lookup.assert_not_awaited()  # type: ignore[attr-defined]
    assert "s" not in provider._sessions


@pytest.mark.asyncio
async def test_ensure_models_loaded_declines_while_unloading() -> None:
    """Models must never be reloaded onto a provider that is on its way out."""
    provider = _make_provider()
    provider._load_models = AsyncMock()  # type: ignore[method-assign]
    provider.unloading = True

    assert await provider.ensure_models_loaded() is False
    provider._load_models.assert_not_awaited()
    assert provider._models_loaded is False


@pytest.mark.asyncio
async def test_ensure_models_loaded_declines_when_queued_behind_unload() -> None:
    """A loader already waiting on the models lock must decline once unload() has run."""
    provider = _make_provider()
    provider.has_unloadable_models = True
    loaded = False

    async def _load() -> None:
        nonlocal loaded
        loaded = True

    provider._load_models = _load  # type: ignore[method-assign]
    provider._free_models = MagicMock()  # type: ignore[method-assign]

    # Hold the lock so the loader is queued behind the unload that follows.
    await provider._models_lock.acquire()
    loader = asyncio.create_task(provider.ensure_models_loaded())
    await asyncio.sleep(0)
    # mass.unload_provider sets this before it awaits provider.unload()
    provider.unloading = True
    provider._models_lock.release()
    await provider.unload()

    assert await loader is False
    assert loaded is False
    assert provider._models_loaded is False
