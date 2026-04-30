"""Tests for the AudioAnalysisController."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _make_streamdetails(*, path: str) -> MagicMock:
    sd = MagicMock()
    sd.path = path
    sd.uri = f"track://test/{path}"
    sd.audio_format = MagicMock()
    sd.audio_format.sample_rate = 44100
    sd.audio_format.bit_depth = 16
    sd.audio_format.channels = 2
    sd.item_id = "test-item"
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
