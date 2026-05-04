"""Tests for the AudioAnalysisController."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import StreamType

import music_assistant.controllers.streams.audio_analysis as audio_analysis_mod
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
    """A provider whose process_pcm_chunk exceeds CHUNK_PROCESS_TIMEOUT is evicted."""
    controller = _make_controller()
    session_key = "track://provider/abc"
    controller._active_sessions[session_key] = {"slow", "fast"}

    async def _hang(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(10)

    slow = _make_aa_provider("slow", available=True, process_pcm_chunk=AsyncMock(side_effect=_hang))
    fast = _make_aa_provider("fast", available=True)
    provider_map = {"slow": slow, "fast": fast}
    controller.mass.get_provider = MagicMock(side_effect=provider_map.get)  # type: ignore[method-assign]

    with patch.object(audio_analysis_mod, "CHUNK_PROCESS_TIMEOUT", 0.05):
        await controller._distribute_chunk(session_key, b"\x00" * 1024)

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


@pytest.mark.asyncio
async def test_get_scan_concurrency_returns_default_on_unset() -> None:
    """When the config value is unset/None, fall back to DEFAULT_BACKGROUND_SCAN_CONCURRENCY."""
    controller = _make_controller()
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == 1


@pytest.mark.asyncio
async def test_get_scan_concurrency_clamps_to_max() -> None:
    """Values above 8 are clamped to 8."""
    controller = _make_controller()
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=99)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == 8


@pytest.mark.asyncio
async def test_get_scan_concurrency_clamps_to_min() -> None:
    """Values below 1 are clamped to 1."""
    controller = _make_controller()
    controller.mass.config.get_raw_core_config_value = MagicMock(return_value=0)  # type: ignore[method-assign]
    assert controller._get_scan_concurrency() == 1


@pytest.mark.asyncio
async def test_background_streaming_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ffmpeg chunks reach providers; session is cleaned up on clean EOF."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/music/test.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    p.finalize = AsyncMock(return_value=None)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    fake_chunks = [b"\x00\x01" * 512 for _ in range(5)]
    fake_ffmpeg = _FakeFFMpeg(chunks=fake_chunks, returncode=0)

    monkeypatch.setattr(
        "music_assistant.controllers.streams.audio_analysis.FFMpeg",
        lambda **_: fake_ffmpeg,
    )

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

    fake_ffmpeg = _FakeFFMpeg(chunks=[b"\x00" * 1024] * 50, returncode=0)
    monkeypatch.setattr(
        "music_assistant.controllers.streams.audio_analysis.FFMpeg",
        lambda **_: fake_ffmpeg,
    )

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
async def test_background_streaming_ffmpeg_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ffmpeg startup failure cancels providers cleanly without raising."""
    controller = _make_controller()
    streamdetails = _make_streamdetails(path="/nonexistent.flac")
    p = _make_aa_provider("p1", available=True)
    p.start_analysis = AsyncMock(return_value=True)
    controller.mass.get_provider = MagicMock(return_value=p)  # type: ignore[method-assign]

    def _ffmpeg_fail(**_kwargs: object) -> None:
        raise RuntimeError("ffmpeg startup failed")

    monkeypatch.setattr("music_assistant.controllers.streams.audio_analysis.FFMpeg", _ffmpeg_fail)

    # Should not raise
    await controller._run_background_streaming_for_track(streamdetails, [p])
    assert streamdetails.uri not in controller._active_sessions
    # Per-track exception must be surfaced to the TasksController.
    controller.mass.tasks.add_task_failure.assert_called_once()  # type: ignore[attr-defined]
    failure_args = controller.mass.tasks.add_task_failure.call_args.args  # type: ignore[attr-defined]
    assert failure_args[0] == audio_analysis_mod.BACKGROUND_SCAN_TASK_ID
    assert "Failed" in failure_args[1]
    assert "ffmpeg startup failed" in failure_args[1]


class _FakeFFMpeg:
    """Minimal FFMpeg stand-in for tests."""

    def __init__(self, chunks: list[bytes], returncode: int = 0) -> None:
        self._chunks = chunks
        self.returncode = returncode
        self.concat_error = None
        self.log_history: list[str] = []

    async def __aenter__(self) -> _FakeFFMpeg:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def iter_chunked(self, _chunk_size: int) -> AsyncGenerator[bytes, None]:
        for chunk in self._chunks:
            yield chunk


def _make_streamdetails(*, path: str, item_id: str = "test-item") -> MagicMock:
    sd = MagicMock()
    sd.path = path
    sd.uri = f"track://test/{path}"
    sd.audio_format = MagicMock()
    sd.audio_format.sample_rate = 44100
    sd.audio_format.bit_depth = 16
    sd.audio_format.channels = 2
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

    async def _track_streaming(streamdetails: MagicMock, _providers: object) -> None:
        streaming_calls.append(streamdetails.item_id)

    monkeypatch.setattr(controller, "_run_background_streaming_for_track", _track_streaming)

    await controller._run_background_scan()

    assert sorted(streaming_calls) == ["track-1", "track-2"]


@pytest.mark.asyncio
async def test_find_candidates_handles_sqlite_row_without_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_candidates_missing_analysis must use __getitem__ not .get() on rows.

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

    rows = [
        _RowNoGet(
            {
                "item_id": "track-1",
                "provider_instance": "filesystem_local",
                "covered_domains": None,  # no analysis yet
            }
        ),
        _RowNoGet(
            {
                "item_id": "track-2",
                "provider_instance": "filesystem_local",
                "covered_domains": "loudness_analysis",  # already covered
            }
        ),
    ]
    controller.mass.music.database.get_rows_from_query = AsyncMock(return_value=rows)  # type: ignore[method-assign]

    result = await controller._find_candidates_missing_analysis(["loudness_analysis"], 100)

    # track-1 is missing loudness_analysis → included
    # track-2 is already covered → excluded
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
        for i in range(5)
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

    async def _track_streaming(_streamdetails: MagicMock, _providers: object) -> None:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1

    monkeypatch.setattr(controller, "_run_background_streaming_for_track", _track_streaming)

    await controller._run_background_scan()

    assert max_in_flight == 2
