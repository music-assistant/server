"""Tests for the AudioAnalysisController."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import AudioFormat

import music_assistant.controllers.streams.audio_analysis as audio_analysis_mod
from music_assistant.constants import DEFAULT_BACKGROUND_SCAN_CONCURRENCY
from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider


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
    """Values above 8 are clamped to 8."""
    controller = _make_controller()
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=99)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == 8


def test_get_scan_concurrency_clamps_to_min() -> None:
    """Values below 1 are clamped to 1."""
    controller = _make_controller()
    # Use a truthy negative value so the controller's `value or DEFAULT` fallback
    # doesn't swap us out for the default before the min-clamp runs.
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=-1)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == 1


def _make_stream_mock(chunks: list[bytes]) -> object:
    """Return a get_media_stream mock that yields the given chunks."""

    async def _stream(
        _streamdetails: object, _pcm_format: object, **_kwargs: object
    ) -> AsyncGenerator[bytes, None]:
        for chunk in chunks:
            yield chunk

    return _stream


@pytest.mark.asyncio
async def test_background_streaming_happy_path() -> None:
    """PCM chunks reach providers; session is cleaned up on clean EOF."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/music/test.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    p.finalize = AsyncMock(return_value=None)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    fake_chunks = [b"\x00\x01" * 512 for _ in range(5)]
    controller.mass.streams.audio.get_media_stream = _make_stream_mock(fake_chunks)  # type: ignore[method-assign,assignment]

    await controller._run_background_streaming_for_track(streamdetails, [p])

    assert p.start_analysis.await_count == 1
    assert p.process_pcm_chunk.await_count == len(fake_chunks)
    # _finalize_providers pops the session key before dispatching — key must be gone
    assert streamdetails.uri not in controller._active_sessions


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
async def test_background_streaming_ffmpeg_startup_failure() -> None:
    """get_media_stream failure cancels providers cleanly without raising."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/nonexistent.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    def _failing_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes, None]:
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


def _make_streamdetails(*, path: str, item_id: str = "test-item") -> MagicMock:
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

    result = await controller._find_candidates_missing_analysis(["loudness_analysis"], 100)

    assert len(result) == 1
    assert result[0]["item_id"] == "track-1"
    assert result[0]["missing_domains"] == ["loudness_analysis"]


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


def _stub_controller(
    count_result: int = 0,
    list_result: list[dict[str, Any]] | None = None,
    rows_from_query_result: list[dict[str, Any]] | None = None,
) -> tuple[AudioAnalysisController, MagicMock]:
    """Build a bare AudioAnalysisController whose database is mocked."""
    c = AudioAnalysisController.__new__(AudioAnalysisController)
    c.logger = MagicMock()
    db = MagicMock()
    db.get_count_from_query = AsyncMock(return_value=count_result)
    db.get_rows = AsyncMock(return_value=list_result or [])
    db.get_rows_from_query = AsyncMock(return_value=rows_from_query_result or [])
    c.mass = MagicMock()
    c.mass.music = MagicMock()
    c.mass.music.database = db
    c.mass.get_providers = MagicMock(return_value=[])
    return c, db


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
async def test_get_audio_analysis_rows_returns_full_rows() -> None:
    """get_audio_analysis_rows forwards what the DB returns; no filtering or parsing."""
    rows: list[dict[str, Any]] = [
        {"item_id": "a", "provider": "filesystem_local", "analysis_data": "{}"},
        {"item_id": "b", "provider": "filesystem_local", "analysis_data": "{}"},
    ]
    c, _ = _stub_controller(list_result=rows)
    result = await c.get_audio_analysis_rows("sonic_analysis")
    assert result == rows


@pytest.mark.asyncio
async def test_get_audio_analysis_rows_filters_by_domain_and_track_media_type() -> None:
    """Default rows query filters on aa_provider_domain + media_type=track and no row limit."""
    c, db = _stub_controller(list_result=[])
    await c.get_audio_analysis_rows("sonic_analysis")
    call = db.get_rows.await_args
    assert call.args[0] == "audio_analysis"
    assert call.args[1] == {
        "aa_provider_domain": "sonic_analysis",
        "media_type": MediaType.TRACK.value,
    }
    assert call.kwargs["limit"] == 0


@pytest.mark.asyncio
async def test_get_audio_analysis_rows_respects_media_type_override() -> None:
    """Caller can list rows for a non-track media type."""
    c, db = _stub_controller(list_result=[])
    await c.get_audio_analysis_rows("sonic_analysis", media_type=MediaType.PODCAST_EPISODE)
    filters = db.get_rows.await_args.args[1]
    assert filters["media_type"] == MediaType.PODCAST_EPISODE.value


@pytest.mark.asyncio
async def test_get_audio_analysis_rows_passes_limit_and_offset() -> None:
    """Caller-supplied limit/offset are forwarded to the DB layer for paginated reads."""
    c, db = _stub_controller(list_result=[])
    await c.get_audio_analysis_rows("sonic_analysis", limit=50, offset=100)
    call = db.get_rows.await_args
    assert call.kwargs["limit"] == 50
    assert call.kwargs["offset"] == 100


def _aa_provider_stub(domain: str, available: bool = True) -> MagicMock:
    """Build a provider stub that satisfies the get_providers().available filter."""
    p = MagicMock()
    p.domain = domain
    p.available = available
    return p


@pytest.mark.asyncio
async def test_get_merged_audio_analysis_rows_merges_within_group() -> None:
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
    c, _ = _stub_controller(rows_from_query_result=rows)
    c.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _aa_provider_stub("sonic_analysis"),
            _aa_provider_stub("smart_fades"),
        ]
    )

    result = await c.get_merged_audio_analysis_rows("sonic_analysis")
    assert len(result) == 1
    item_id, provider, merged = result[0]
    assert (item_id, provider) == ("t1", "filesystem_local")
    assert merged.bpm == 120.0  # smart_fades wins on bpm (later row)
    assert merged.energy == 0.5  # sonic_analysis still wins where smart_fades is None
    assert merged.key == "C"


@pytest.mark.asyncio
async def test_get_merged_audio_analysis_rows_skips_unavailable_providers() -> None:
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
    c, _ = _stub_controller(rows_from_query_result=rows)
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = await c.get_merged_audio_analysis_rows("sonic_analysis")
    assert len(result) == 1
    assert result[0][2].bpm == 100.0  # disabled_provider's row ignored


@pytest.mark.asyncio
async def test_get_merged_audio_analysis_rows_groups_by_item_provider() -> None:
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
    c, _ = _stub_controller(rows_from_query_result=rows)
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = await c.get_merged_audio_analysis_rows("sonic_analysis")
    assert len(result) == 2
    assert {r[0] for r in result} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_get_merged_audio_analysis_rows_skips_unparsable_rows() -> None:
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
    c, _ = _stub_controller(rows_from_query_result=rows)
    c.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _aa_provider_stub("sonic_analysis"),
            _aa_provider_stub("smart_fades"),
        ]
    )

    result = await c.get_merged_audio_analysis_rows("sonic_analysis")
    assert len(result) == 1
    assert result[0][2].bpm == 120.0


@pytest.mark.asyncio
async def test_get_merged_audio_analysis_rows_empty_db_returns_empty_list() -> None:
    """An empty DB result yields an empty list without flushing a sentinel group."""
    c, _ = _stub_controller(rows_from_query_result=[])
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = await c.get_merged_audio_analysis_rows("sonic_analysis")
    assert result == []


@pytest.mark.asyncio
async def test_get_merged_audio_analysis_rows_drops_groups_with_only_corrupt_rows() -> None:
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
    c, _ = _stub_controller(rows_from_query_result=rows)
    c.mass.get_providers = MagicMock(return_value=[_aa_provider_stub("sonic_analysis")])  # type: ignore[method-assign]

    result = await c.get_merged_audio_analysis_rows("sonic_analysis")
    assert len(result) == 1
    assert result[0][0] == "good"


def _make_aa_provider_with_domain(
    domain: str,
    *,
    available: bool = True,
    provider_status: dict[str, Any] | None = None,
    analysis_version: int = 1,
) -> MagicMock:
    """AA provider mock with domain, get_provider_status, and analysis_version set."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.domain = domain
    provider.available = available
    provider.analysis_version = analysis_version
    provider.get_provider_status = AsyncMock(return_value=provider_status or {})
    return provider


@pytest.mark.asyncio
async def test_status_returns_common_fields_for_known_aa_domain() -> None:
    """status() returns provider_loaded, analyzed_tracks_count, analysis_version."""
    c, _ = _stub_controller(count_result=42)
    p = _make_aa_provider_with_domain("loudness_analysis", analysis_version=2)
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.status(aa_domain="loudness_analysis")

    assert result["provider_loaded"] is True
    assert result["analyzed_tracks_count"] == 42
    assert result["analysis_version"] == 2
    c.mass.get_provider.assert_called_once_with(
        "loudness_analysis", provider_type=AudioAnalysisProvider
    )


@pytest.mark.asyncio
async def test_status_merges_provider_status_extras() -> None:
    """status() merges the provider's get_provider_status extras into the response."""
    c, _ = _stub_controller(count_result=10)
    p = _make_aa_provider_with_domain(
        "sonic_analysis",
        provider_status={"clap_model_loaded": True},
        analysis_version=3,
    )
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.status(aa_domain="sonic_analysis")

    assert result["clap_model_loaded"] is True
    assert result["analyzed_tracks_count"] == 10
    assert result["analysis_version"] == 3


@pytest.mark.asyncio
async def test_status_raises_for_unknown_aa_domain() -> None:
    """Unknown aa_domain raises ProviderUnavailableError."""
    c, _ = _stub_controller()
    c.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(ProviderUnavailableError):
        await c.status(aa_domain="nope")


@pytest.mark.asyncio
async def test_analyzed_tracks_passes_limit_and_offset_to_db() -> None:
    """analyzed_tracks() passes limit/offset to the row helper."""
    c, _ = _stub_controller(count_result=0, list_result=[])
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]
    c.mass.music.tracks = MagicMock()
    c.mass.music.tracks.get = AsyncMock(side_effect=Exception("not used; no rows"))

    result = await c.analyzed_tracks(aa_domain="sonic_analysis", limit=10, offset=20)

    assert result == {"total": 0, "offset": 20, "limit": 10, "items": []}


@pytest.mark.asyncio
async def test_analyzed_tracks_dedupes_within_page_and_resolves_metadata() -> None:
    """Rows duplicated by (item_id, provider) are deduped; surviving entries get track lookup."""
    rows = [
        {"item_id": "a", "provider": "filesystem_local"},
        {"item_id": "a", "provider": "filesystem_local"},
        {"item_id": "b", "provider": "filesystem_local"},
    ]
    c, _ = _stub_controller(count_result=3, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    fake_track = MagicMock(name="t")
    fake_track.name = "Track Name"
    fake_track.artists = []
    c.mass.music.tracks = MagicMock()
    c.mass.music.tracks.get = AsyncMock(return_value=fake_track)

    result = await c.analyzed_tracks(aa_domain="sonic_analysis", limit=50, offset=0)

    assert result["total"] == 3
    assert len(result["items"]) == 2
    assert {item["item_id"] for item in result["items"]} == {"a", "b"}


@pytest.mark.asyncio
async def test_analyzed_tracks_search_is_page_scoped_substring() -> None:
    """Search filters page rows on item_id substring (case-insensitive)."""
    rows = [
        {"item_id": "rock_track_1", "provider": "filesystem_local"},
        {"item_id": "jazz_track_2", "provider": "filesystem_local"},
        {"item_id": "ROCK_track_3", "provider": "filesystem_local"},
        {"item_id": "pop_track_4", "provider": "filesystem_local"},
    ]
    c, _ = _stub_controller(count_result=4, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    fake_track = MagicMock(name="t")
    fake_track.name = "n"
    fake_track.artists = []
    c.mass.music.tracks = MagicMock()
    c.mass.music.tracks.get = AsyncMock(return_value=fake_track)

    result = await c.analyzed_tracks(aa_domain="sonic_analysis", search="rock", limit=10, offset=0)

    assert result["total"] == 4
    assert {item["item_id"] for item in result["items"]} == {"rock_track_1", "ROCK_track_3"}
    assert c.mass.music.tracks.get.await_count == 2


@pytest.mark.asyncio
async def test_analyzed_tracks_raises_for_unknown_aa_domain() -> None:
    """Unknown aa_domain raises ProviderUnavailableError."""
    c, _ = _stub_controller()
    c.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(ProviderUnavailableError):
        await c.analyzed_tracks(aa_domain="nope")


@pytest.mark.asyncio
async def test_export_returns_fixed_scalar_field_set() -> None:
    """export() returns the canonical scalar fields from analysis_data."""
    rows = [
        {
            "item_id": "track1",
            "provider": "filesystem_local",
            "analysis_data": json.dumps(
                {
                    "bpm": 120.5,
                    "key": "C",
                    "danceability": 0.7,
                    "energy": 0.8,
                    "loudness_integrated": -8.4,
                    "duration": 213.7,
                }
            ),
        }
    ]
    c, _ = _stub_controller(count_result=1, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.export(aa_domain="sonic_analysis", limit=10, offset=0)

    assert result["total"] == 1
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["bpm"] == 120.5
    assert item["key"] == "C"
    assert item["danceability"] == 0.7
    assert item["loudness_integrated"] == -8.4


@pytest.mark.asyncio
async def test_export_omits_extra_data_by_default() -> None:
    """Default export response carries no extra_data key at all."""
    rows = [
        {
            "item_id": "a",
            "provider": "filesystem_local",
            "analysis_data": json.dumps(
                {"bpm": 120, "extra_data": {"clap_embedding": [0.1, 0.2], "foo": 1}}
            ),
        }
    ]
    c, _ = _stub_controller(count_result=1, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.export(aa_domain="sonic_analysis")

    assert "extra_data" not in result["items"][0]
    assert result["items"][0]["bpm"] == 120


@pytest.mark.asyncio
async def test_export_includes_full_unmodified_extra_data_when_opted_in() -> None:
    """include_extra_data=True returns the full extra_data blob, embedding included."""
    rows = [
        {
            "item_id": "a",
            "provider": "filesystem_local",
            "analysis_data": json.dumps(
                {"bpm": 120, "extra_data": {"clap_embedding": [0.1, 0.2], "foo": 1}}
            ),
        }
    ]
    c, _ = _stub_controller(count_result=1, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.export(aa_domain="sonic_analysis", include_extra_data=True)

    assert result["items"][0]["extra_data"] == {"clap_embedding": [0.1, 0.2], "foo": 1}


@pytest.mark.asyncio
async def test_export_skips_unparseable_rows() -> None:
    """Rows with corrupt JSON are skipped from items but counted in total."""
    rows = [
        {"item_id": "a", "provider": "filesystem_local", "analysis_data": "not json"},
        {
            "item_id": "b",
            "provider": "filesystem_local",
            "analysis_data": json.dumps({"bpm": 100.0}),
        },
    ]
    c, _ = _stub_controller(count_result=5, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.export(aa_domain="sonic_analysis", limit=10, offset=0)
    assert result["total"] == 5
    assert len(result["items"]) == 1
    assert result["items"][0]["bpm"] == 100.0


@pytest.mark.asyncio
async def test_export_passes_limit_and_offset_to_db() -> None:
    """export() forwards limit/offset to the row helper and returns an empty page when there are no rows."""
    c, _ = _stub_controller(count_result=0, list_result=[])
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]
    result = await c.export(aa_domain="sonic_analysis", limit=25, offset=50)
    assert result == {"total": 0, "offset": 50, "limit": 25, "items": []}


@pytest.mark.asyncio
async def test_export_raises_for_unknown_aa_domain() -> None:
    """Unknown aa_domain raises ProviderUnavailableError."""
    c, _ = _stub_controller()
    c.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(ProviderUnavailableError):
        await c.export(aa_domain="nope")


@pytest.mark.asyncio
async def test_export_omits_null_fields() -> None:
    """Fields with null values in analysis_data are omitted from the response."""
    rows = [
        {
            "item_id": "track1",
            "provider": "filesystem_local",
            "analysis_data": json.dumps(
                {
                    "bpm": 120.5,
                    "key": None,
                    "mode": None,
                    "energy": 0.8,
                }
            ),
        }
    ]
    c, _ = _stub_controller(count_result=1, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.export(aa_domain="sonic_analysis", limit=10, offset=0)
    item = result["items"][0]
    assert "bpm" in item
    assert "energy" in item
    assert "key" not in item
    assert "mode" not in item


@pytest.mark.asyncio
async def test_export_rounds_float_fields_to_four_decimals() -> None:
    """Float values are rounded to 4 decimal places (matches legacy contract)."""
    rows = [
        {
            "item_id": "track1",
            "provider": "filesystem_local",
            "analysis_data": json.dumps(
                {
                    "energy": 0.123456789,
                    "danceability": 0.987654321,
                }
            ),
        }
    ]
    c, _ = _stub_controller(count_result=1, list_result=rows)
    p = _make_aa_provider_with_domain("sonic_analysis")
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    result = await c.export(aa_domain="sonic_analysis", limit=10, offset=0)
    item = result["items"][0]
    assert item["energy"] == 0.1235
    assert item["danceability"] == 0.9877


@pytest.mark.asyncio
async def test_track_returns_full_record_including_extra_data() -> None:
    """track() returns the complete parsed analysis dict, embedding included."""
    full = {"bpm": 120, "extra_data": {"clap_embedding": [0.1, 0.2, 0.3]}}
    c, _ = _stub_controller()
    p = _make_aa_provider_with_domain("sonic_analysis")
    music_prov = MagicMock()
    music_prov.is_streaming_provider = False
    music_prov.instance_id = "filesystem_local"

    def _get_provider(dom: str, provider_type: object = None) -> MagicMock:  # noqa: ARG001
        return p if dom == "sonic_analysis" else music_prov

    c.mass.get_provider = MagicMock(side_effect=_get_provider)  # type: ignore[method-assign]
    c.mass.music.database.get_row = AsyncMock(return_value={"analysis_data": json.dumps(full)})

    result = await c.track(
        aa_domain="sonic_analysis",
        item_id="a",
        provider_instance_id_or_domain="filesystem_local",
    )

    assert result == full


@pytest.mark.asyncio
async def test_track_returns_none_when_no_row() -> None:
    """No stored analysis row -> None (not an error)."""
    c, _ = _stub_controller()
    p = _make_aa_provider_with_domain("sonic_analysis")
    music_prov = MagicMock()
    music_prov.is_streaming_provider = False
    music_prov.instance_id = "filesystem_local"

    def _get_provider(dom: str, provider_type: object = None) -> MagicMock:  # noqa: ARG001
        return p if dom == "sonic_analysis" else music_prov

    c.mass.get_provider = MagicMock(side_effect=_get_provider)  # type: ignore[method-assign]
    c.mass.music.database.get_row = AsyncMock(return_value=None)

    result = await c.track(
        aa_domain="sonic_analysis",
        item_id="missing",
        provider_instance_id_or_domain="filesystem_local",
    )

    assert result is None


@pytest.mark.asyncio
async def test_track_returns_none_when_music_provider_unresolved() -> None:
    """AA provider resolves but the music provider lookup returns None -> None."""
    c, _ = _stub_controller()
    p = _make_aa_provider_with_domain("sonic_analysis")

    def _get_provider(dom: str, provider_type: object = None) -> MagicMock | None:  # noqa: ARG001
        return p if dom == "sonic_analysis" else None

    c.mass.get_provider = MagicMock(side_effect=_get_provider)  # type: ignore[method-assign]

    result = await c.track(
        aa_domain="sonic_analysis",
        item_id="a",
        provider_instance_id_or_domain="filesystem_local",
    )

    assert result is None


@pytest.mark.asyncio
async def test_track_returns_none_when_analysis_data_unparseable() -> None:
    """A stored row with unparseable analysis_data -> None (not an error)."""
    c, _ = _stub_controller()
    p = _make_aa_provider_with_domain("sonic_analysis")
    music_prov = MagicMock()
    music_prov.is_streaming_provider = False
    music_prov.instance_id = "filesystem_local"

    def _get_provider(dom: str, provider_type: object = None) -> MagicMock:  # noqa: ARG001
        return p if dom == "sonic_analysis" else music_prov

    c.mass.get_provider = MagicMock(side_effect=_get_provider)  # type: ignore[method-assign]
    c.mass.music.database.get_row = AsyncMock(return_value={"analysis_data": "not valid json {"})

    result = await c.track(
        aa_domain="sonic_analysis",
        item_id="a",
        provider_instance_id_or_domain="filesystem_local",
    )

    assert result is None


@pytest.mark.asyncio
async def test_track_raises_for_unknown_aa_domain() -> None:
    """Unloaded AA provider raises ProviderUnavailableError."""
    c, _ = _stub_controller()
    c.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(ProviderUnavailableError):
        await c.track(
            aa_domain="nope",
            item_id="a",
            provider_instance_id_or_domain="filesystem_local",
        )


@pytest.mark.asyncio
async def test_coverage_returns_three_counts_and_version() -> None:
    """coverage() reports analyzed, pending, stale_version, analysis_version."""
    c, _ = _stub_controller()
    p = _make_aa_provider_with_domain("sonic_analysis", analysis_version=3)
    c.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]
    c.get_audio_analysis_count = AsyncMock(return_value=100)  # type: ignore[method-assign]
    c._count_candidates_missing_analysis = AsyncMock(return_value=20)  # type: ignore[method-assign]
    c.mass.music.database.get_count_from_query = AsyncMock(return_value=5)

    result = await c.coverage(aa_domain="sonic_analysis")

    assert result == {
        "analyzed": 100,
        "pending": 20,
        "stale_version": 5,
        "analysis_version": 3,
    }


@pytest.mark.asyncio
async def test_coverage_raises_for_unknown_aa_domain() -> None:
    """Unloaded AA provider raises ProviderUnavailableError."""
    c, _ = _stub_controller()
    c.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(ProviderUnavailableError):
        await c.coverage(aa_domain="nope")


@pytest.mark.asyncio
async def test_count_candidates_missing_analysis_zero_without_filesystem() -> None:
    """No available filesystem music providers -> 0 pending (no DB query)."""
    c, _ = _stub_controller()
    c.mass.get_providers = MagicMock(return_value=[])  # type: ignore[method-assign]

    assert await c._count_candidates_missing_analysis("sonic_analysis") == 0


@pytest.mark.asyncio
async def test_count_candidates_missing_analysis_queries_with_available_filesystem() -> None:
    """With an available filesystem provider, the NOT EXISTS count query runs with bound params."""
    c, db = _stub_controller(count_result=7)
    domain = next(iter(audio_analysis_mod.FILESYSTEM_PROVIDER_DOMAINS))
    fs_prov = MagicMock()
    fs_prov.domain = domain
    fs_prov.available = True
    c.mass.get_providers = MagicMock(return_value=[fs_prov])  # type: ignore[method-assign]

    result = await c._count_candidates_missing_analysis("sonic_analysis")

    assert result == 7
    db.get_count_from_query.assert_awaited_once()
    sql, params = db.get_count_from_query.await_args.args
    assert "NOT EXISTS" in sql
    assert f"'{domain}'" in sql
    assert params == {"media_type": MediaType.TRACK.value, "aa_domain": "sonic_analysis"}


def test_controller_has_no_provider_specific_extra_data_keys() -> None:
    """Generic controller must not reference any provider extra_data key names."""
    source = inspect.getsource(audio_analysis_mod)
    assert "_EXPORT_STRIP_EXTRA_DATA_KEYS" not in source
    assert "clap_embedding" not in source
