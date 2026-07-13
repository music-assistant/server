"""Tests for the AudioAnalysisController."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import AsyncGenerator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from music_assistant_models.audio_analysis import AudioAnalysisCoverage
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track

import music_assistant.controllers.streams.audio_analysis as audio_analysis_mod
from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
    _default_background_scan_concurrency,
)
from music_assistant.controllers.streams.audio_analysis import (
    LOUDNESS_ANALYSIS_DOMAIN,
    SMART_FADES_ANALYSIS_DOMAIN,
    SONIC_ANALYSIS_DOMAIN,
    AudioAnalysisController,
    _merged_from_rows,
)
from music_assistant.controllers.streams.audio_buffer import AudioBufferEOF
from music_assistant.helpers.json import json_dumps
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import (
    AudioAnalysisProvider,
    InstrumentedSemaphore,
)
from music_assistant.models.music_provider import MusicProvider


@pytest.mark.asyncio
async def test_distribute_chunk_calls_all_providers() -> None:
    """_distribute_chunk must invoke process_pcm_chunk on every active provider."""
    controller = _make_controller()
    session_key = "track://provider/abc"
    controller._active_sessions[session_key] = {"prov-1", "prov-2"}

    p1 = _make_aa_provider("prov-1", available=True)
    p2 = _make_aa_provider("prov-2", available=True)
    provider_map = {"prov-1": p1, "prov-2": p2}
    controller.mass.get_provider = MagicMock(side_effect=provider_map.get)  # type: ignore[method-assign]

    await controller._distribute_chunk(session_key, b"\x00" * 1024)

    p1.process_pcm_chunk.assert_awaited_once_with(session_key, b"\x00" * 1024)
    p2.process_pcm_chunk.assert_awaited_once_with(session_key, b"\x00" * 1024)


def test_ensure_inference_runtime_configured_is_idempotent() -> None:
    """The inference runtime (torch + native BLAS caps) is configured once per controller."""
    controller = _make_controller()
    with (
        patch("torch.set_num_threads") as set_threads,
        patch("torch.set_num_interop_threads"),
        patch("threadpoolctl.threadpool_limits") as blas_limits,
        patch("torch.backends.nnpack.set_flags"),
    ):
        controller.ensure_inference_runtime_configured()
        controller.ensure_inference_runtime_configured()
    set_threads.assert_called_once()
    blas_limits.assert_called_once()
    if controller.analysis_executor is not None:
        controller.analysis_executor.shutdown(wait=False)


def test_ensure_inference_runtime_creates_solo_lock_and_executor() -> None:
    """Runtime config creates the playback-priority solo lock and a dedicated worker pool."""
    controller = _make_controller()
    with (
        patch("torch.set_num_threads"),
        patch("torch.set_num_interop_threads"),
        patch("threadpoolctl.threadpool_limits"),
        patch("torch.backends.nnpack.set_flags"),
    ):
        controller.ensure_inference_runtime_configured()
    try:
        assert isinstance(controller.analysis_solo_lock, asyncio.Lock)
        assert isinstance(controller.analysis_executor, ThreadPoolExecutor)
    finally:
        if controller.analysis_executor is not None:
            controller.analysis_executor.shutdown(wait=False)


def test_playback_active_delegates_to_streams() -> None:
    """playback_active reflects the streams controller's active-output-stream gauge."""
    controller = _make_controller()
    controller.streams.output_stream_active = MagicMock(return_value=True)  # type: ignore[method-assign]
    assert controller.playback_active() is True
    controller.streams.output_stream_active = MagicMock(return_value=False)  # type: ignore[method-assign]
    assert controller.playback_active() is False


@pytest.mark.parametrize(
    ("cpu_count", "expected_permits"),
    [(2, 1), (4, 2), (8, 4), (16, 8)],
)
@pytest.mark.asyncio
async def test_analysis_concurrency_capped_at_half_cores(
    cpu_count: int, expected_permits: int
) -> None:
    """The analysis concurrency cap is half the cores (min 1) on every host."""
    controller = _make_controller()
    with (
        patch(
            "music_assistant.controllers.streams.audio_analysis.os.process_cpu_count",
            return_value=cpu_count,
        ),
        patch("torch.set_num_threads"),
        patch("torch.set_num_interop_threads"),
        patch("threadpoolctl.threadpool_limits"),
        patch("torch.backends.nnpack.set_flags"),
    ):
        controller.ensure_inference_runtime_configured()
    semaphore = controller.analysis_semaphore
    assert isinstance(semaphore, asyncio.Semaphore)
    # Exactly `expected_permits` acquires exhaust the cap.
    for _ in range(expected_permits):
        await semaphore.acquire()
    assert semaphore.locked()


@pytest.mark.asyncio
async def test_instrumented_semaphore_tracks_in_flight_and_waiters() -> None:
    """InstrumentedSemaphore exposes live permit-in-use and queued-acquirer counts."""
    sem = InstrumentedSemaphore(2)
    assert (sem.capacity, sem.in_flight, sem.waiters) == (2, 0, 0)

    await sem.acquire()
    await sem.acquire()
    assert sem.in_flight == 2
    assert sem.locked()

    # A third acquire blocks behind the cap and registers as a waiter.
    blocked = asyncio.ensure_future(sem.acquire())
    await asyncio.sleep(0)
    assert sem.waiters == 1
    assert sem.in_flight == 2

    # Freeing a permit lets the queued acquirer through; the queue drains.
    sem.release()
    await blocked
    assert sem.waiters == 0
    assert sem.in_flight == 2

    sem.release()
    sem.release()
    assert sem.in_flight == 0


@pytest.mark.asyncio
async def test_distribute_chunk_evicts_provider_on_timeout() -> None:
    """A provider whose process_pcm_chunk exceeds max_interval is evicted."""
    controller = _make_controller()
    session_key = "track://provider/abc"
    controller._active_sessions[session_key] = {"slow", "fast"}

    async def _hang(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(10)

    slow = _make_aa_provider("slow", available=True, process_pcm_chunk=AsyncMock(side_effect=_hang))
    fast = _make_aa_provider("fast", available=True)
    provider_map = {"slow": slow, "fast": fast}
    controller.mass.get_provider = MagicMock(side_effect=provider_map.get)  # type: ignore[method-assign]

    await controller._distribute_chunk(session_key, b"\x00" * 1024, max_interval=0.05)

    assert "slow" not in controller._active_sessions[session_key]
    assert "fast" in controller._active_sessions[session_key]


@pytest.mark.asyncio
async def test_distribute_chunk_evicts_provider_on_exception() -> None:
    """A provider that raises in process_pcm_chunk is evicted; others continue."""
    controller = _make_controller()
    session_key = "track://provider/abc"
    controller._active_sessions[session_key] = {"raises", "ok"}

    raises = _make_aa_provider(
        "raises",
        available=True,
        process_pcm_chunk=AsyncMock(side_effect=RuntimeError("boom")),
    )
    ok = _make_aa_provider("ok", available=True)
    provider_map = {"raises": raises, "ok": ok}
    controller.mass.get_provider = MagicMock(side_effect=provider_map.get)  # type: ignore[method-assign]

    await controller._distribute_chunk(session_key, b"\x00" * 1024)

    assert "raises" not in controller._active_sessions[session_key]
    assert "ok" in controller._active_sessions[session_key]


def test_get_scan_concurrency_returns_default_on_unset() -> None:
    """When the config value is unset/None, fall back to DEFAULT_BACKGROUND_SCAN_CONCURRENCY."""
    controller = _make_controller()
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == DEFAULT_BACKGROUND_SCAN_CONCURRENCY


def test_get_scan_concurrency_clamps_to_max() -> None:
    """Values above 16 are clamped to 16."""
    controller = _make_controller()
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=99)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == 16


def test_get_scan_concurrency_clamps_to_min() -> None:
    """Values below 1 are clamped to 1."""
    controller = _make_controller()
    # Use a truthy negative value so the controller's `value or DEFAULT` fallback
    # doesn't swap us out for the default before the min-clamp runs.
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=-1)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == 1


@pytest.mark.parametrize(
    ("cpu_count", "expected"),
    [(1, 1), (2, 1), (3, 1), (4, 2), (8, 2), (16, 2)],
)
def test_default_background_scan_concurrency(cpu_count: int, expected: int) -> None:
    """Background scan defaults to 1 below 4 cores, 2 at/above (never more than 2)."""
    with patch("music_assistant.constants.os.process_cpu_count", return_value=cpu_count):
        assert _default_background_scan_concurrency() == expected


def _make_stream_mock(chunks: list[bytes]) -> object:
    """Return a get_media_stream mock that yields the given chunks."""

    async def _stream(
        _streamdetails: object, _pcm_format: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        for chunk in chunks:
            yield chunk

    return _stream


@pytest.mark.asyncio
async def test_background_streaming_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """PCM chunks reach providers; session is cleaned up on clean EOF."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/music/test.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    p.finalize = AsyncMock(return_value=None)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    fake_chunks = [b"\x00\x01" * 512 for _ in range(5)]
    controller.mass.streams.audio.get_media_stream = _make_stream_mock(fake_chunks)  # type: ignore[method-assign,assignment]
    monkeypatch.setattr(audio_analysis_mod, "BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR", 0.0)

    await controller._run_background_streaming_for_track(streamdetails, [p])

    assert p.start_analysis.await_count == 1
    assert p.process_pcm_chunk.await_count == len(fake_chunks)
    # _finalize_providers pops the session key before dispatching — key must be gone
    assert streamdetails.uri not in controller._active_sessions


@pytest.mark.asyncio
async def test_background_streaming_paces_chunk_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background dispatch enforces the pacing floor between consecutive chunks."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/music/test.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    p.finalize = AsyncMock(return_value=None)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    fake_chunks = [b"\x00\x01" * 512 for _ in range(4)]
    controller.mass.streams.audio.get_media_stream = _make_stream_mock(fake_chunks)  # type: ignore[method-assign,assignment]
    monkeypatch.setattr(audio_analysis_mod, "BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR", 0.05)

    started = asyncio.get_running_loop().time()
    await controller._run_background_streaming_for_track(streamdetails, [p])
    elapsed = asyncio.get_running_loop().time() - started

    assert p.process_pcm_chunk.await_count == len(fake_chunks)
    # First chunk dispatches immediately; each of the remaining 3 waits out the floor.
    assert elapsed >= 3 * 0.05


@pytest.mark.asyncio
async def test_background_streaming_per_track_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-track timeout cancels providers and cleans up the session."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/music/test.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)

    async def _hang_chunk(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(10)

    p.process_pcm_chunk = AsyncMock(side_effect=_hang_chunk)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    controller.mass.streams.audio.get_media_stream = _make_stream_mock([b"\x00" * 1024] * 50)  # type: ignore[method-assign,assignment]
    monkeypatch.setattr(audio_analysis_mod, "BACKGROUND_PER_TRACK_TIMEOUT_SECONDS", 0.2)

    await controller._run_background_streaming_for_track(streamdetails, [p])

    assert streamdetails.uri not in controller._active_sessions
    # Per-track timeout must be surfaced to the TasksController so the run ends
    # as PARTIAL_SUCCESS with a retryable status.
    controller.mass.tasks.add_task_failure.assert_called_once()  # type: ignore[attr-defined]
    failure_args = controller.mass.tasks.add_task_failure.call_args.args  # type: ignore[attr-defined]
    assert failure_args[0] == audio_analysis_mod.BACKGROUND_SCAN_TASK_ID
    assert "Timed out" in failure_args[1]
    assert streamdetails.uri in failure_args[1]


@pytest.mark.asyncio
async def test_background_streaming_timeout_scales_with_track_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-track timeout is derived from track duration when duration is known."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/music/long_mix.flac", duration=3600)
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    p.finalize = AsyncMock(return_value=None)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]
    controller.mass.streams.audio.get_media_stream = _make_stream_mock([])  # type: ignore[method-assign,assignment]

    captured_timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _spy_wait_for(coro: Any, timeout: float | None) -> Any:
        captured_timeouts.append(timeout)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr("asyncio.wait_for", _spy_wait_for)

    await controller._run_background_streaming_for_track(streamdetails, [p])

    expected = int(3600 * audio_analysis_mod.BACKGROUND_PER_TRACK_TIMEOUT_DURATION_MULTIPLIER)
    assert captured_timeouts[0] == expected


@pytest.mark.asyncio
async def test_background_streaming_ffmpeg_startup_failure() -> None:
    """get_media_stream failure cancels providers cleanly without raising."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/nonexistent.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    def _failing_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        raise RuntimeError("ffmpeg startup failed")

    controller.mass.streams.audio.get_media_stream = _failing_stream  # type: ignore[method-assign]

    # Should not raise
    await controller._run_background_streaming_for_track(streamdetails, [p])
    assert streamdetails.uri not in controller._active_sessions
    # Per-track exception must be surfaced to the TasksController.
    controller.mass.tasks.add_task_failure.assert_called_once()  # type: ignore[attr-defined]
    failure_args = controller.mass.tasks.add_task_failure.call_args.args  # type: ignore[attr-defined]
    assert failure_args[0] == audio_analysis_mod.BACKGROUND_SCAN_TASK_ID
    assert "Failed" in failure_args[1]
    assert "ffmpeg startup failed" in failure_args[1]


def _make_streamdetails(
    *, path: str, item_id: str = "test-item", duration: int | None = None
) -> MagicMock:
    sd = MagicMock()
    sd.path = path
    sd.uri = f"track://test/{path}"
    sd.audio_format = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    sd.item_id = item_id
    sd.provider = "test-provider"
    sd.media_type = MagicMock()
    sd.duration = duration
    return sd


def _make_controller() -> AudioAnalysisController:
    streams = MagicMock()
    streams.mass = MagicMock()
    streams.mass.logger.getChild.return_value = MagicMock()
    return AudioAnalysisController(streams)


def _make_aa_provider(
    instance_id: str,
    *,
    available: bool = True,
    process_pcm_chunk: AsyncMock | None = None,
) -> MagicMock:
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.instance_id = instance_id
    provider.available = available
    provider.process_pcm_chunk = process_pcm_chunk or AsyncMock(return_value=None)
    provider.cancel = AsyncMock(return_value=None)
    return provider


@pytest.mark.asyncio
async def test_run_background_scan_uses_union_candidate_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new scan loop drives _run_background_streaming_for_track per candidate."""
    controller = _make_controller()
    p1 = _make_aa_provider("prov-1", available=True)
    p1.domain = "p1"
    p1.start_analysis = AsyncMock(return_value=True)
    monkeypatch.setattr(
        controller.__class__,
        "providers",
        property(lambda _self: [p1]),
    )

    candidates = [
        {"item_id": "track-1", "provider_instance": "filesystem_local", "missing_domains": ["p1"]},
        {"item_id": "track-2", "provider_instance": "filesystem_local", "missing_domains": ["p1"]},
    ]
    monkeypatch.setattr(
        controller, "_find_candidates_missing_analysis", AsyncMock(return_value=candidates)
    )

    streamdetails_list = [
        _make_streamdetails(path=f"/music/{c['item_id']}.flac", item_id=str(c["item_id"]))
        for c in candidates
    ]
    for sd in streamdetails_list:
        sd.stream_type = StreamType.LOCAL_FILE

    music_prov = MagicMock()
    music_prov.available = True
    music_prov.get_stream_details = AsyncMock(side_effect=streamdetails_list)
    music_prov.instance_id = "filesystem_local"
    controller.mass.get_provider = MagicMock(return_value=music_prov)  # type: ignore[method-assign]

    streaming_calls: list[str] = []

    async def _track_streaming(
        streamdetails: MagicMock, _providers: object, **_kwargs: object
    ) -> None:
        streaming_calls.append(streamdetails.item_id)

    monkeypatch.setattr(controller, "_run_background_streaming_for_track", _track_streaming)

    await controller._run_background_scan()

    assert sorted(streaming_calls) == ["track-1", "track-2"]


@pytest.mark.asyncio
async def test_find_candidates_handles_sqlite_row_without_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    _find_candidates_missing_analysis must use __getitem__ not .get() on rows.

    sqlite3.Row supports only __getitem__, not .get(). This regression test
    uses a row class that lacks .get() to ensure we never reintroduce the bug.
    """
    controller = _make_controller()
    p1 = _make_aa_provider("prov-1", available=True)
    p1.domain = "loudness_analysis"
    p1.analysis_version = 1
    p1.available = True
    monkeypatch.setattr(
        controller.__class__,
        "providers",
        property(lambda _self: [p1]),
    )

    # Make the filesystem-providers gate succeed
    fs_prov = MagicMock()
    fs_prov.domain = "filesystem_local"
    fs_prov.available = True
    controller.mass.get_providers = MagicMock(return_value=[fs_prov])  # type: ignore[method-assign]

    class _RowNoGet:
        """Mimics sqlite3.Row: __getitem__ only, no .get()."""

        def __init__(self, data: dict[str, object]) -> None:
            self._d = data

        def __getitem__(self, key: str) -> object:
            return self._d[key]

    # SQL filters out fully-covered tracks via NOT EXISTS + GROUP BY, so the
    # rows we receive from the database only contain missing-domain pairs.
    rows = [
        _RowNoGet(
            {
                "item_id": "track-1",
                "provider_instance": "filesystem_local",
                "missing_domains": "loudness_analysis",
            }
        ),
    ]
    controller.mass.music.database.get_rows_from_query = AsyncMock(return_value=rows)  # type: ignore[method-assign]

    result = await controller._find_candidates_missing_analysis({"loudness_analysis": 1}, 100)

    assert len(result) == 1
    assert result[0]["item_id"] == "track-1"
    assert result[0]["missing_domains"] == ["loudness_analysis"]


@pytest.mark.asyncio
async def test_find_candidates_query_gates_on_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The candidate query must treat stale-version rows as needing re-analysis.

    The NOT EXISTS gate may only count a stored analysis row as up-to-date when
    its analysis_version is non-NULL and >= the provider's current version, so a
    provider bumping analysis_version re-surfaces previously analyzed tracks.
    """
    controller = _make_controller()
    p1 = _make_aa_provider("prov-1", available=True)
    p1.domain = "sonic_analysis"
    monkeypatch.setattr(
        controller.__class__,
        "providers",
        property(lambda _self: [p1]),
    )

    fs_prov = MagicMock()
    fs_prov.domain = "filesystem_local"
    fs_prov.available = True
    controller.mass.get_providers = MagicMock(return_value=[fs_prov])  # type: ignore[method-assign]

    captured: dict[str, Any] = {}

    async def _capture(query: str, params: dict[str, Any], limit: int) -> list[Any]:  # noqa: ARG001
        captured["query"] = query
        captured["params"] = params
        return []

    controller.mass.music.database.get_rows_from_query = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    await controller._find_candidates_missing_analysis({"sonic_analysis": 3}, 0)

    sql = captured["query"]
    assert "aa.analysis_version IS NOT NULL" in sql
    assert "aa.analysis_version >= possible.current_version" in sql
    assert captured["params"]["ver_0"] == 3
    assert captured["params"]["aa_0"] == "sonic_analysis"


@pytest.mark.asyncio
async def test_run_background_scan_concurrency_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At most CONF_BACKGROUND_SCAN_CONCURRENCY tracks run concurrently."""
    controller = _make_controller()
    monkeypatch.setattr(controller, "_get_scan_concurrency", lambda: 2)

    p1 = _make_aa_provider("prov-1", available=True)
    p1.domain = "p1"
    p1.start_analysis = AsyncMock(return_value=True)
    monkeypatch.setattr(
        controller.__class__,
        "providers",
        property(lambda _self: [p1]),
    )

    candidates = [
        {
            "item_id": f"track-{i}",
            "provider_instance": "filesystem_local",
            "missing_domains": ["p1"],
        }
        for i in range(4)
    ]
    monkeypatch.setattr(
        controller, "_find_candidates_missing_analysis", AsyncMock(return_value=candidates)
    )

    streamdetails_list = [
        _make_streamdetails(path=f"/music/{c['item_id']}.flac") for c in candidates
    ]
    for sd in streamdetails_list:
        sd.stream_type = StreamType.LOCAL_FILE
    music_prov = MagicMock()
    music_prov.available = True
    music_prov.get_stream_details = AsyncMock(side_effect=streamdetails_list)
    music_prov.instance_id = "filesystem_local"
    controller.mass.get_provider = MagicMock(return_value=music_prov)  # type: ignore[method-assign]

    in_flight = 0
    max_in_flight = 0
    barrier = asyncio.Barrier(2)

    async def _track_streaming(
        _streamdetails: MagicMock, _providers: object, **_kwargs: object
    ) -> None:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await barrier.wait()
        in_flight -= 1

    monkeypatch.setattr(controller, "_run_background_streaming_for_track", _track_streaming)

    await controller._run_background_scan()

    assert max_in_flight == 2


@pytest.mark.asyncio
async def test_background_streaming_cancellation_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError mid-track must trigger _cancel_providers and re-raise."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/music/test.flac")
    p = _make_aa_provider("p1", available=True)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    session_key = streamdetails.uri

    async def _inner_cancelled(
        _session_key: str, _sd: object, _providers: object, **_kwargs: object
    ) -> None:
        # Simulate the inner having registered the session before being cancelled.
        controller._active_sessions[session_key] = {"p1"}
        raise asyncio.CancelledError

    monkeypatch.setattr(controller, "_run_background_streaming_inner", _inner_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await controller._run_background_streaming_for_track(streamdetails, [p])

    # Session must be popped and provider.cancel scheduled.
    assert session_key not in controller._active_sessions
    p.cancel.assert_called_once_with(session_key)


@pytest.mark.asyncio
async def test_run_background_scan_defers_past_run_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracks past the run-budget deadline are deferred to the next run."""
    controller = _make_controller()

    p1 = _make_aa_provider("prov-1", available=True)
    p1.domain = "p1"
    monkeypatch.setattr(
        controller.__class__,
        "providers",
        property(lambda _self: [p1]),
    )

    candidates = [
        {
            "item_id": f"track-{i}",
            "provider_instance": "filesystem_local",
            "missing_domains": ["p1"],
        }
        for i in range(3)
    ]
    monkeypatch.setattr(
        controller, "_find_candidates_missing_analysis", AsyncMock(return_value=candidates)
    )

    # Force budget to negative so every candidate is past deadline.
    monkeypatch.setattr(audio_analysis_mod, "BACKGROUND_SCAN_RUN_BUDGET_SECONDS", -1)

    streaming_called = False

    async def _track_streaming(_sd: object, _providers: object, **_kwargs: object) -> None:
        nonlocal streaming_called
        streaming_called = True

    monkeypatch.setattr(controller, "_run_background_streaming_for_track", _track_streaming)

    await controller._run_background_scan()

    assert not streaming_called


@pytest.mark.asyncio
async def test_close_drains_sessions_and_workers() -> None:
    """close() cancels in-flight chunk workers and dispatches provider cancels."""
    controller = _make_controller()

    # Real asyncio task that swallows cancellation cleanly.
    async def _busy_worker() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    worker_task = asyncio.create_task(_busy_worker())
    controller._workers["track://test/a"] = worker_task

    p = _make_aa_provider("p1", available=True)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]
    controller._active_sessions["track://test/a"] = {"p1"}

    await controller.close()

    # Worker awaited to completion; both dicts drained.
    assert worker_task.done()
    assert controller._workers == {}
    assert controller._active_sessions == {}
    # Provider cancel scheduled with the session key.
    p.cancel.assert_called_once_with("track://test/a")


async def _run_buffer_reader_worker(
    chunk_count: int, expected_duration: float | None
) -> tuple[MagicMock, MagicMock]:
    """Run the reader worker against a buffer yielding chunk_count 1s chunks, then EOF."""
    controller = _make_controller()
    session_key = "track://test/worker"
    controller._active_sessions[session_key] = {"prov-1"}
    controller._distribute_chunk = AsyncMock()  # type: ignore[method-assign]
    controller._finalize_providers = MagicMock()  # type: ignore[method-assign]
    controller._cancel_providers = MagicMock()  # type: ignore[method-assign]
    audio_buffer = MagicMock()
    audio_buffer.first_buffered_chunk = 0
    audio_buffer.read_chunk_for_analysis = AsyncMock(
        side_effect=[b"pcm"] * chunk_count + [AudioBufferEOF()]
    )
    await controller._buffer_reader_worker(session_key, audio_buffer, expected_duration)
    return controller._finalize_providers, controller._cancel_providers


@pytest.mark.asyncio
async def test_buffer_reader_worker_finalizes_when_near_expected_duration() -> None:
    """An EOF within the completeness tolerance finalizes the session."""
    finalize, cancel = await _run_buffer_reader_worker(9, expected_duration=10)
    finalize.assert_called_once_with("track://test/worker")
    cancel.assert_not_called()


@pytest.mark.asyncio
async def test_buffer_reader_worker_discards_incomplete_stream() -> None:
    """A source that ends far short of the expected duration is cancelled, not finalized."""
    finalize, cancel = await _run_buffer_reader_worker(8, expected_duration=10)
    finalize.assert_not_called()
    cancel.assert_called_once_with("track://test/worker")


@pytest.mark.asyncio
async def test_buffer_reader_worker_finalizes_when_duration_unknown() -> None:
    """Without an expected duration (e.g. radio), any clean EOF finalizes the session."""
    finalize, cancel = await _run_buffer_reader_worker(1, expected_duration=None)
    finalize.assert_called_once_with("track://test/worker")
    cancel.assert_not_called()


def _stub_controller(
    count_result: int = 0,
    iter_rows: list[dict[str, Any]] | None = None,
) -> tuple[AudioAnalysisController, MagicMock]:
    """Build a bare AudioAnalysisController whose database is mocked."""
    c = AudioAnalysisController.__new__(AudioAnalysisController)
    c.logger = MagicMock()
    db = MagicMock()
    db.get_count_from_query = AsyncMock(return_value=count_result)
    db.delete = AsyncMock()
    rows_to_yield = list(iter_rows or [])

    async def _iter_stub(*_args: Any, **_kwargs: Any) -> AsyncGenerator[Mapping[str, Any]]:
        for row in rows_to_yield:
            yield row

    db.iter_rows_from_query = MagicMock(side_effect=_iter_stub)
    c.mass = MagicMock()
    c.mass.music = MagicMock()
    c.mass.music.database = db
    c.mass.get_providers = MagicMock(return_value=[])
    return c, db


# --- provider-scoped merge (loudness regression fix) ---

_ALL_AA_DOMAINS = {LOUDNESS_ANALYSIS_DOMAIN, SMART_FADES_ANALYSIS_DOMAIN, SONIC_ANALYSIS_DOMAIN}


def _aa_row(domain: str, row_id: int, **fields: Any) -> dict[str, Any]:
    """Build one audio_analysis db row (rows are passed oldest-first / ascending row_id)."""
    return {
        "id": row_id,
        "item_id": "track-1",
        "provider": "test-provider",
        "media_type": MediaType.TRACK.value,
        "aa_provider_domain": domain,
        "analysis_data": json_dumps(AudioAnalysisData(**fields).to_dict()),
    }


def test_merged_from_rows_priority_none_is_last_write_wins() -> None:
    """Without priority, the newest (last) row wins each non-None field (legacy behaviour)."""
    rows = [
        _aa_row(LOUDNESS_ANALYSIS_DOMAIN, 1, loudness_integrated=-7.5),
        _aa_row(SONIC_ANALYSIS_DOMAIN, 2, loudness_integrated=-12.0),
    ]
    merged = _merged_from_rows(rows, _ALL_AA_DOMAINS)
    assert merged is not None
    assert merged.loudness_integrated == -12.0


def test_merged_from_rows_single_priority_uses_only_that_provider() -> None:
    """A single-domain priority returns only that provider's values; others are ignored."""
    rows = [
        _aa_row(LOUDNESS_ANALYSIS_DOMAIN, 1, loudness_integrated=-7.5),
        _aa_row(SONIC_ANALYSIS_DOMAIN, 2, loudness_integrated=-12.0, bpm=120),
    ]
    merged = _merged_from_rows(rows, _ALL_AA_DOMAINS, priority=(LOUDNESS_ANALYSIS_DOMAIN,))
    assert merged is not None
    assert merged.loudness_integrated == -7.5
    assert merged.bpm is None  # sonic_analysis excluded entirely


def test_merged_from_rows_multi_priority_first_listed_wins_and_merges() -> None:
    """Multi-domain priority merges all listed domains; the first-listed wins conflicts."""
    rows = [
        # sonic newer than loudness, but loudness is listed first -> wins loudness_integrated
        _aa_row(SONIC_ANALYSIS_DOMAIN, 1, loudness_integrated=-12.0, energy=0.5),
        _aa_row(LOUDNESS_ANALYSIS_DOMAIN, 2, loudness_integrated=-7.5),
        _aa_row(SMART_FADES_ANALYSIS_DOMAIN, 3, bpm=120),
    ]
    merged = _merged_from_rows(
        rows,
        _ALL_AA_DOMAINS,
        priority=(LOUDNESS_ANALYSIS_DOMAIN, SONIC_ANALYSIS_DOMAIN, SMART_FADES_ANALYSIS_DOMAIN),
    )
    assert merged is not None
    assert merged.loudness_integrated == -7.5  # first-listed wins the conflict
    assert merged.energy == 0.5  # non-conflicting field from sonic still merged in
    assert merged.bpm == 120  # and from smart_fades


def test_merged_from_rows_priority_domain_not_available_is_excluded() -> None:
    """A priority domain that is not currently available is dropped (can yield None)."""
    rows = [_aa_row(LOUDNESS_ANALYSIS_DOMAIN, 1, loudness_integrated=-7.5)]
    merged = _merged_from_rows(rows, {SONIC_ANALYSIS_DOMAIN}, priority=(LOUDNESS_ANALYSIS_DOMAIN,))
    assert merged is None


def test_merged_from_rows_regression_sonic_does_not_clobber_loudness() -> None:
    """
    Regression: sonic_analysis' RMS loudness must not overwrite the EBU R128 value.

    Reproduces the volume-jump bug: a newer sonic_analysis row carries an RMS-proxy
    loudness_integrated that wins under last-write-wins, but scoping to loudness_analysis
    returns the authoritative value.
    """
    rows = [
        _aa_row(LOUDNESS_ANALYSIS_DOMAIN, 1, loudness_integrated=-7.5),
        _aa_row(SONIC_ANALYSIS_DOMAIN, 2, loudness_integrated=-12.0),
    ]
    legacy = _merged_from_rows(rows, _ALL_AA_DOMAINS)
    assert legacy is not None
    assert legacy.loudness_integrated == -12.0  # old/buggy: sonic clobbers
    scoped = _merged_from_rows(rows, _ALL_AA_DOMAINS, priority=(LOUDNESS_ANALYSIS_DOMAIN,))
    assert scoped is not None
    assert scoped.loudness_integrated == -7.5  # fixed


@pytest.mark.asyncio
async def test_get_audio_analysis_priority_threads_through_to_merge() -> None:
    """get_audio_analysis forwards priority so the loudness call gets the EBU R128 value."""
    c, db = _stub_controller()
    db.get_rows = AsyncMock(
        return_value=[
            _aa_row(LOUDNESS_ANALYSIS_DOMAIN, 1, loudness_integrated=-7.5),
            _aa_row(SONIC_ANALYSIS_DOMAIN, 2, loudness_integrated=-12.0),
        ]
    )
    music_prov = MagicMock(spec=MusicProvider)
    music_prov.is_streaming_provider = True
    music_prov.domain = "test-provider"
    c.mass.get_provider = MagicMock(return_value=music_prov)  # type: ignore[method-assign]
    aa_loud = MagicMock()
    aa_loud.domain = LOUDNESS_ANALYSIS_DOMAIN
    aa_loud.available = True
    aa_sonic = MagicMock()
    aa_sonic.domain = SONIC_ANALYSIS_DOMAIN
    aa_sonic.available = True
    c.mass.get_providers = MagicMock(return_value=[aa_loud, aa_sonic])  # type: ignore[method-assign]

    result = await c.get_audio_analysis(
        "track-1", "test-provider", priority=(LOUDNESS_ANALYSIS_DOMAIN,)
    )
    assert result is not None
    assert result.loudness_integrated == -7.5


@pytest.mark.asyncio
async def test_get_audio_analysis_count_returns_helper_result() -> None:
    """The controller forwards whatever get_count_from_query returns."""
    c, _ = _stub_controller(count_result=42)
    assert await c.get_audio_analysis_count("sonic_analysis") == 42


@pytest.mark.asyncio
async def test_get_audio_analysis_count_filters_by_domain_and_track_media_type() -> None:
    """Default count filters on aa_provider_domain AND media_type=track."""
    c, db = _stub_controller(count_result=0)
    await c.get_audio_analysis_count("sonic_analysis")
    sql, params = db.get_count_from_query.await_args.args
    assert "aa_provider_domain = :aa_provider_domain" in sql
    assert "media_type = :media_type" in sql
    assert params == {"aa_provider_domain": "sonic_analysis", "media_type": MediaType.TRACK.value}


@pytest.mark.asyncio
async def test_get_audio_analysis_count_respects_media_type_override() -> None:
    """Caller can count rows for a non-track media type."""
    c, db = _stub_controller(count_result=7)
    result = await c.get_audio_analysis_count(
        "sonic_analysis", media_type=MediaType.PODCAST_EPISODE
    )
    assert result == 7
    params = db.get_count_from_query.await_args.args[1]
    assert params["media_type"] == MediaType.PODCAST_EPISODE.value


@pytest.mark.asyncio
async def test_iter_audio_analysis_rows_yields_all_rows() -> None:
    """iter_audio_analysis_rows yields each DB row in order; no filtering or parsing."""
    rows: list[dict[str, Any]] = [
        {"item_id": "a", "provider": "filesystem_local", "analysis_data": "{}"},
        {"item_id": "b", "provider": "filesystem_local", "analysis_data": "{}"},
    ]
    c, _ = _stub_controller(iter_rows=rows)
    result = [r async for r in c.iter_audio_analysis_rows("sonic_analysis")]
    assert result == rows


@pytest.mark.asyncio
async def test_iter_audio_analysis_rows_filters_by_domain_and_track_media_type() -> None:
    """Default query filters on aa_provider_domain + media_type=track."""
    c, db = _stub_controller(iter_rows=[])
    [r async for r in c.iter_audio_analysis_rows("sonic_analysis")]
    sql, params = db.iter_rows_from_query.call_args.args
    assert "aa_provider_domain = :aa_provider_domain" in sql
    assert "media_type = :media_type" in sql
    assert params == {
        "aa_provider_domain": "sonic_analysis",
        "media_type": MediaType.TRACK.value,
    }


@pytest.mark.asyncio
async def test_iter_audio_analysis_rows_respects_media_type_override() -> None:
    """Caller can stream rows for a non-track media type."""
    c, db = _stub_controller(iter_rows=[])
    [
        r
        async for r in c.iter_audio_analysis_rows(
            "sonic_analysis", media_type=MediaType.PODCAST_EPISODE
        )
    ]
    params = db.iter_rows_from_query.call_args.args[1]
    assert params["media_type"] == MediaType.PODCAST_EPISODE.value


def _aa_provider_stub(domain: str, available: bool = True) -> MagicMock:
    """Build a provider stub that satisfies the get_providers().available filter."""
    p = MagicMock()
    p.domain = domain
    p.available = available
    return p


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_merges_within_group() -> None:
    """Two rows for the same (item_id, provider) merge in timestamp order."""
    rows: list[dict[str, Any]] = [
        {
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": '{"bpm": 100.0, "energy": 0.5}',
        },
        {
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "smart_fades",
            "analysis_data": '{"bpm": 120.0, "key": "C"}',
        },
    ]
    c, _ = _stub_controller(iter_rows=rows)
    c.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _aa_provider_stub("sonic_analysis"),
            _aa_provider_stub("smart_fades"),
        ]
    )

    result = [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]
    assert len(result) == 1
    item_id, provider, merged = result[0]
    assert (item_id, provider) == ("t1", "filesystem_local")
    assert merged.bpm == 120.0  # smart_fades wins on bpm (later row)
    assert merged.energy == 0.5  # sonic_analysis still wins where smart_fades is None
    assert merged.key == "C"


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_skips_unavailable_providers() -> None:
    """Rows from unavailable AA providers are skipped during merge."""
    rows: list[dict[str, Any]] = [
        {
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": '{"bpm": 100.0}',
        },
        {
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "disabled_provider",
            "analysis_data": '{"bpm": 999.0}',
        },
    ]
    c, _ = _stub_controller(iter_rows=rows)
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]
    assert len(result) == 1
    assert result[0][2].bpm == 100.0  # disabled_provider's row ignored


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_groups_by_item_provider() -> None:
    """Rows from different (item_id, provider) pairs are emitted as separate entries."""
    rows: list[dict[str, Any]] = [
        {
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": '{"bpm": 100.0}',
        },
        {
            "item_id": "t2",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": '{"bpm": 200.0}',
        },
    ]
    c, _ = _stub_controller(iter_rows=rows)
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]
    assert len(result) == 2
    assert {r[0] for r in result} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_skips_unparsable_rows() -> None:
    """A row with corrupt JSON is silently skipped without aborting the merge."""
    rows: list[dict[str, Any]] = [
        {
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": "not-json",
        },
        {
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "smart_fades",
            "analysis_data": '{"bpm": 120.0}',
        },
    ]
    c, _ = _stub_controller(iter_rows=rows)
    c.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _aa_provider_stub("sonic_analysis"),
            _aa_provider_stub("smart_fades"),
        ]
    )

    result = [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]
    assert len(result) == 1
    assert result[0][2].bpm == 120.0


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_empty_db_yields_nothing() -> None:
    """An empty DB result yields no entries without flushing a sentinel group."""
    c, _ = _stub_controller(iter_rows=[])
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]
    assert result == []


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_logs_warning_for_unparsable_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unparsable rows must surface a WARNING so storage corruption is observable."""
    rows: list[dict[str, Any]] = [
        {
            "id": 42,
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": "not-json",
        },
        {
            "id": 43,
            "item_id": "t1",
            "provider": "filesystem_local",
            "aa_provider_domain": "smart_fades",
            "analysis_data": '{"bpm": 120.0}',
        },
    ]
    c, _ = _stub_controller(iter_rows=rows)
    c.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _aa_provider_stub("sonic_analysis"),
            _aa_provider_stub("smart_fades"),
        ]
    )

    with caplog.at_level("WARNING", logger=audio_analysis_mod.LOGGER.name):
        [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]

    assert any(
        "Skipping unparsable audio_analysis row" in r.message
        and "id=42" in r.message
        and "sonic_analysis" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_drops_groups_with_only_corrupt_rows() -> None:
    """A group whose only row has corrupt JSON is not emitted at all."""
    rows: list[dict[str, Any]] = [
        {
            "item_id": "broken",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": "not-json",
        },
        {
            "item_id": "good",
            "provider": "filesystem_local",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": '{"bpm": 100.0}',
        },
    ]
    c, _ = _stub_controller(iter_rows=rows)
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]
    assert len(result) == 1
    assert result[0][0] == "good"


@pytest.mark.asyncio
async def test_iter_merged_audio_analysis_rows_warns_when_primary_domain_offline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Offline primary AA domain yields nothing and surfaces a WARNING."""
    c, db = _stub_controller(iter_rows=[])
    # Only smart_fades is available; sonic_analysis (the queried domain) isn't.
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("smart_fades")])  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger=audio_analysis_mod.LOGGER.name):
        result = [x async for x in c.iter_merged_audio_analysis_rows("sonic_analysis")]

    assert result == []
    # Early return must short-circuit before any DB work.
    assert not db.iter_rows_from_query.called
    assert any(
        "offline primary AA domain" in r.message and "sonic_analysis" in r.message
        for r in caplog.records
    )


def _make_aa_provider_with_domain(
    domain: str,
    *,
    available: bool = True,
    analysis_version: int = 1,
) -> MagicMock:
    """AA provider mock with domain and analysis_version set."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.domain = domain
    provider.available = available
    provider.analysis_version = analysis_version
    return provider


@pytest.mark.asyncio
async def test_coverage_returns_three_counts_and_version() -> None:
    """get_coverage() reports analyzed, pending, stale_version, analysis_version."""
    c, _ = _stub_controller()
    p = _make_aa_provider_with_domain("sonic_analysis", analysis_version=3)
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]
    c.get_audio_analysis_count = AsyncMock(return_value=100)  # type: ignore[method-assign]
    c._count_candidates_missing_analysis = AsyncMock(return_value=20)  # type: ignore[method-assign]
    c.mass.music.database.get_count_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=5
    )

    result = await c.get_coverage(aa_domain="sonic_analysis")

    assert result == AudioAnalysisCoverage(
        analyzed=100,
        pending=20,
        stale_version=5,
        analysis_version=3,
    )


@pytest.mark.asyncio
async def test_coverage_raises_for_unknown_aa_domain() -> None:
    """Unloaded AA provider raises ProviderUnavailableError."""
    c, _ = _stub_controller()
    c.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(ProviderUnavailableError):
        await c.get_coverage(aa_domain="nope")


@pytest.mark.asyncio
async def test_coverage_stale_query_counts_null_analysis_version_as_stale() -> None:
    """Rows with NULL analysis_version must be counted as stale (SQLite `NULL < N` is NULL)."""
    c, db = _stub_controller(count_result=0)
    p = _make_aa_provider_with_domain("sonic_analysis", analysis_version=3)
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]
    c.get_audio_analysis_count = AsyncMock(return_value=0)  # type: ignore[method-assign]
    c._count_candidates_missing_analysis = AsyncMock(return_value=0)  # type: ignore[method-assign]

    await c.get_coverage(aa_domain="sonic_analysis")

    sql, params = db.get_count_from_query.await_args.args
    assert "analysis_version IS NULL" in sql
    assert "analysis_version < :current_version" in sql
    assert params == {
        "aa_domain": "sonic_analysis",
        "media_type": MediaType.TRACK.value,
        "current_version": 3,
    }


@pytest.mark.asyncio
async def test_count_candidates_missing_analysis_zero_without_filesystem() -> None:
    """No available filesystem music providers -> 0 pending (no DB query)."""
    c, _ = _stub_controller()
    c.mass.get_providers = MagicMock(return_value=[])  # type: ignore[method-assign]

    assert await c._count_candidates_missing_analysis("sonic_analysis", 1) == 0


@pytest.mark.asyncio
async def test_count_candidates_missing_analysis_queries_with_available_filesystem() -> None:
    """With an available filesystem provider, the NOT EXISTS count query runs with bound params."""
    c, db = _stub_controller(count_result=7)
    domain = next(iter(audio_analysis_mod.FILESYSTEM_PROVIDER_DOMAINS))
    fs_prov = MagicMock()
    fs_prov.domain = domain
    fs_prov.available = True
    c.mass.get_providers = MagicMock(return_value=[fs_prov])  # type: ignore[method-assign]

    result = await c._count_candidates_missing_analysis("sonic_analysis", 2)

    assert result == 7
    db.get_count_from_query.assert_awaited_once()
    sql, params = db.get_count_from_query.await_args.args
    assert "NOT EXISTS" in sql
    assert "aa.analysis_version IS NOT NULL" in sql
    assert "aa.analysis_version >= :current_version" in sql
    assert f"'{domain}'" in sql
    assert params["media_type"] == MediaType.TRACK.value
    assert params["aa_domain"] == "sonic_analysis"
    assert params["current_version"] == 2
    assert "now" in params
    assert "aa.analysis_version IS NOT NULL" in sql
    assert "aa.analysis_version >= :current_version" in sql


def test_controller_has_no_provider_specific_extra_data_keys() -> None:
    """Generic controller must not reference any provider extra_data key names."""
    source = inspect.getsource(audio_analysis_mod)
    # Deliberately a raw source-substring guard: the generic controller must never
    # name provider specifics, even in a comment. The brittleness is intentional --
    # do not weaken this to an import/attribute check.
    assert "_EXPORT_STRIP_EXTRA_DATA_KEYS" not in source
    assert "clap_embedding" not in source


# --- track audio metadata & waveform export ---


def _track_with_mapping(item_id: str = "track-1", provider: str = "test-provider") -> Track:
    """Build a Track with a single provider mapping."""
    return Track(
        item_id=item_id,
        provider="library",
        name="Test Track",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
            )
        },
    )


def _analysis_controller_with_rows(
    rows: list[Mapping[str, Any]],
) -> AudioAnalysisController:
    """Build a stub controller whose DB returns the given analysis rows for any track."""
    c, db = _stub_controller()
    db.get_rows = AsyncMock(return_value=rows)
    music_prov = MagicMock(spec=MusicProvider)
    music_prov.is_streaming_provider = True
    music_prov.domain = "test-provider"
    c.mass.get_provider = MagicMock(return_value=music_prov)  # type: ignore[method-assign]
    c.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _aa_provider_stub(SMART_FADES_ANALYSIS_DOMAIN),
            _aa_provider_stub(SONIC_ANALYSIS_DOMAIN),
        ]
    )
    return c


@pytest.mark.asyncio
async def test_get_track_audio_metadata_skips_corrupt_sqlite_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt sqlite3.Row is logged and skipped without hiding valid analysis."""
    corrupt_data = json_dumps({"spectral_centroid": [100.0, None]})
    valid_data = json_dumps(AudioAnalysisData(bpm=128.0).to_dict())
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        rows = cast(
            "list[Mapping[str, Any]]",
            db.execute(
                """
                SELECT 1 AS id, ? AS aa_provider_domain, ? AS analysis_data
                UNION ALL
                SELECT 2, ?, ?
                """,
                (
                    SONIC_ANALYSIS_DOMAIN,
                    corrupt_data,
                    SMART_FADES_ANALYSIS_DOMAIN,
                    valid_data,
                ),
            ).fetchall(),
        )

    controller = _analysis_controller_with_rows(rows)
    with caplog.at_level("WARNING", logger=audio_analysis_mod.LOGGER.name):
        result = await controller.get_track_audio_metadata(_track_with_mapping())

    assert result is not None
    assert result.bpm == 128.0
    warning = next(
        record for record in caplog.records if record.name == audio_analysis_mod.LOGGER.name
    )
    assert "id=1, domain=sonic_analysis" in warning.getMessage()
    assert corrupt_data not in warning.getMessage()
    assert warning.exc_info is None


@pytest.mark.asyncio
async def test_get_audio_analysis_deletes_unparsable_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt rows are deleted, so their stored version no longer blocks re-analysis."""
    rows: list[Mapping[str, Any]] = [
        {
            "id": 7,
            "aa_provider_domain": SMART_FADES_ANALYSIS_DOMAIN,
            "analysis_data": json_dumps({"spectral_centroid": [100.0, None]}),
        },
        _aa_row(SONIC_ANALYSIS_DOMAIN, 8, bpm=101.0),
    ]
    controller = _analysis_controller_with_rows(rows)
    with caplog.at_level("WARNING", logger=audio_analysis_mod.LOGGER.name):
        result = await controller.get_audio_analysis("track-1", "test-provider")

    assert result is not None
    assert result.bpm == 101.0
    delete_mock = cast("AsyncMock", controller.mass.music.database.delete)
    delete_mock.assert_awaited_once_with(DB_TABLE_AUDIO_ANALYSIS, {"id": 7})
    warning = next(r for r in caplog.records if r.name == audio_analysis_mod.LOGGER.name)
    assert "in field spectral_centroid" in warning.getMessage()


@pytest.mark.asyncio
async def test_set_audio_analysis_rejects_non_finite_values() -> None:
    """A payload holding non-finite floats is refused before anything reaches the database."""
    c, db = _stub_controller()
    list_case = AudioAnalysisData(spectral_centroid=[100.0, float("nan"), 200.0])
    with pytest.raises(ValueError, match="spectral_centroid"):
        await c.set_audio_analysis(
            "track-1", "test-provider", SMART_FADES_ANALYSIS_DOMAIN, list_case
        )
    scalar_case = AudioAnalysisData(bpm=float("inf"))
    with pytest.raises(ValueError, match="bpm"):
        await c.set_audio_analysis(
            "track-1", "test-provider", SMART_FADES_ANALYSIS_DOMAIN, scalar_case
        )
    db.insert_or_replace.assert_not_called()


@pytest.mark.asyncio
async def test_get_track_audio_metadata_prefers_smart_fades() -> None:
    """bpm/key come from smart_fades even when another AA provider wrote them later."""
    c = _analysis_controller_with_rows(
        [
            _aa_row(SMART_FADES_ANALYSIS_DOMAIN, 1, bpm=128.0, key="F#", mode="minor"),
            _aa_row(SONIC_ANALYSIS_DOMAIN, 2, bpm=100.0),
        ]
    )
    result = await c.get_track_audio_metadata(_track_with_mapping())
    assert result is not None
    assert result.bpm == 128.0
    assert result.musical_key == "F# minor"


@pytest.mark.asyncio
async def test_get_track_audio_metadata_key_without_mode() -> None:
    """musical_key falls back to the bare pitch class when no mode was detected."""
    c = _analysis_controller_with_rows([_aa_row(SMART_FADES_ANALYSIS_DOMAIN, 1, key="C")])
    result = await c.get_track_audio_metadata(_track_with_mapping())
    assert result is not None
    assert result.bpm is None
    assert result.musical_key == "C"


@pytest.mark.asyncio
async def test_get_track_audio_metadata_none_without_relevant_analysis() -> None:
    """No AudioMetadata when stored analysis has neither bpm nor key."""
    c = _analysis_controller_with_rows(
        [_aa_row(SONIC_ANALYSIS_DOMAIN, 1, loudness_integrated=-7.5)]
    )
    assert await c.get_track_audio_metadata(_track_with_mapping()) is None


@pytest.mark.asyncio
async def test_get_wave_form_returns_rms_bins() -> None:
    """wave_form returns the stored RMS energy bins as a plain list of floats."""
    rms = np.linspace(0.0, 1.0, 1800, dtype=np.float32).tolist()
    c = _analysis_controller_with_rows([_aa_row(SMART_FADES_ANALYSIS_DOMAIN, 1, rms_energy=rms)])
    result = await c.get_wave_form("track-1", "test-provider")
    assert result is not None
    assert len(result) == 1800
    assert result[0] == pytest.approx(0.0)
    assert result[-1] == pytest.approx(1.0)
    assert all(isinstance(v, float) for v in result)


@pytest.mark.asyncio
async def test_get_wave_form_none_without_rms() -> None:
    """wave_form returns None when no AA provider stored RMS energy."""
    c = _analysis_controller_with_rows([_aa_row(SMART_FADES_ANALYSIS_DOMAIN, 1, bpm=120.0)])
    assert await c.get_wave_form("track-1", "test-provider") is None
