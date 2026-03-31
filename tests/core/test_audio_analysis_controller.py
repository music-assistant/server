"""Tests for the AudioAnalysisController."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio_analysis import (
    AudioAnalysisController,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.audio_analysis_provider import (
    AnalysisSessionData,
    AudioAnalysisProvider,
)

TEST_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)
ONE_SECOND_CHUNK = b"\x00" * TEST_PCM_FORMAT.pcm_sample_size


async def _make_source(num_chunks: int) -> AsyncGenerator[bytes, None]:
    for i in range(num_chunks):
        yield bytes([i % 256]) * TEST_PCM_FORMAT.pcm_sample_size


def _create_mock_provider(
    instance_id: str = "prov_1",
    name: str = "TestProvider",
    domain: str = "test_domain",
) -> MagicMock:
    prov = MagicMock(spec=AudioAnalysisProvider)
    prov.instance_id = instance_id
    prov.name = name
    prov.domain = domain
    prov.available = True
    prov.start_analysis = AsyncMock()
    prov.process_pcm_chunk = AsyncMock()
    prov.finalize = AsyncMock()
    prov.cancel = AsyncMock()
    return prov


@pytest.fixture
def mock_provider() -> MagicMock:
    """Return a single mock AudioAnalysisProvider."""
    return _create_mock_provider()


@pytest.fixture
def mock_mass(mock_provider: MagicMock) -> MagicMock:
    """Return a mock MusicAssistant instance wired to mock_provider."""
    mass = MagicMock()
    mass.create_task = MagicMock(side_effect=lambda coro: asyncio.ensure_future(coro))
    mass.get_providers = MagicMock(return_value=[mock_provider])
    mass.get_provider = MagicMock(return_value=mock_provider)
    mass.music.get_audio_analysis_version = AsyncMock(return_value=None)
    return mass


@pytest.fixture
def mock_streams(mock_mass: MagicMock) -> MagicMock:
    """Return a mock StreamsController with mass attached."""
    streams = MagicMock()
    streams.mass = mock_mass
    return streams


@pytest.fixture
def mock_stream_details() -> MagicMock:
    """Return mock StreamDetails for a test track."""
    sd = MagicMock()
    sd.seek_position = 0
    sd.provider = "test_prov"
    sd.media_type = "track"
    sd.item_id = "test_123"
    sd.uri = "test_prov://track/test_123"
    return sd


@pytest.fixture
def controller(mock_streams: MagicMock) -> AudioAnalysisController:
    """Return an AudioAnalysisController wired to mock_streams."""
    return AudioAnalysisController(mock_streams)


@pytest.fixture
def audio_buffer() -> AudioBuffer:
    """Return a fresh AudioBuffer with test PCM format."""
    return AudioBuffer(TEST_PCM_FORMAT)


# -- Early returns --


@pytest.mark.asyncio
async def test_start_analysis_no_providers(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """No providers available means no callbacks registered and no sessions."""
    mock_mass.get_providers.return_value = []
    await controller.start_analysis(audio_buffer, mock_stream_details)
    assert len(audio_buffer._chunk_callbacks) == 0
    assert len(controller._active_sessions) == 0


@pytest.mark.asyncio
async def test_start_analysis_duplicate_session(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
) -> None:
    """Second call with the same stream details is a no-op."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    assert len(controller._active_sessions) == 1

    buf2 = AudioBuffer(TEST_PCM_FORMAT)
    await controller.start_analysis(buf2, mock_stream_details)
    # No extra callbacks on the second buffer
    assert len(buf2._chunk_callbacks) == 0
    # Still only one session
    assert len(controller._active_sessions) == 1


@pytest.mark.asyncio
async def test_start_analysis_all_providers_fail(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """All providers raising on start_analysis means no session is created."""
    mock_provider.start_analysis.side_effect = RuntimeError("init failed")
    await controller.start_analysis(audio_buffer, mock_stream_details)
    assert len(controller._active_sessions) == 0
    assert len(audio_buffer._chunk_callbacks) == 0


# -- Happy path --


@pytest.mark.asyncio
async def test_chunks_delivered_to_provider(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """Provider receives all PCM chunks via the worker."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_make_source(3), source_name="test")
    await asyncio.sleep(0.3)
    assert mock_provider.process_pcm_chunk.call_count == 3


@pytest.mark.asyncio
async def test_finalize_called_on_eof(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """After EOF, provider.finalize is called with the session key."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_make_source(2), source_name="test")
    await asyncio.sleep(0.3)
    session_key = "test_prov:track:test_123"
    mock_provider.finalize.assert_called_once_with(session_key)


@pytest.mark.asyncio
async def test_multiple_providers_receive_chunks(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """Two providers both get all chunks and finalize."""
    prov_a = _create_mock_provider(instance_id="prov_a", name="ProvA")
    prov_b = _create_mock_provider(instance_id="prov_b", name="ProvB")
    mock_mass.get_providers.return_value = [prov_a, prov_b]

    def _get_prov(pid: str) -> MagicMock:
        return {"prov_a": prov_a, "prov_b": prov_b}.get(pid, prov_a)

    mock_mass.get_provider = MagicMock(side_effect=_get_prov)

    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_make_source(3), source_name="test")
    await asyncio.sleep(0.3)

    assert prov_a.process_pcm_chunk.call_count == 3
    assert prov_b.process_pcm_chunk.call_count == 3
    prov_a.finalize.assert_called_once()
    prov_b.finalize.assert_called_once()


@pytest.mark.asyncio
async def test_session_cleaned_up_after_finalize(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
) -> None:
    """Internal dicts are empty after finalize completes."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_make_source(1), source_name="test")
    await asyncio.sleep(0.3)
    assert len(controller._active_sessions) == 0
    assert len(controller._queues) == 0
    assert len(controller._workers) == 0


# -- Cancel path --


@pytest.mark.asyncio
async def test_cancel_on_buffer_clear(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """Clearing the buffer triggers provider.cancel."""

    async def _slow_source() -> AsyncGenerator[bytes, None]:
        for i in range(10):
            yield bytes([i % 256]) * TEST_PCM_FORMAT.pcm_sample_size
            await asyncio.sleep(0.1)

    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_slow_source(), source_name="test")
    # Let a couple chunks arrive, then cancel before EOF
    await asyncio.sleep(0.25)
    await audio_buffer.clear()
    await asyncio.sleep(0.1)
    session_key = "test_prov:track:test_123"
    mock_provider.cancel.assert_called_once_with(session_key)


@pytest.mark.asyncio
async def test_session_cleaned_up_after_cancel(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
) -> None:
    """Internal dicts are empty after cancel."""

    async def _slow_source() -> AsyncGenerator[bytes, None]:
        for i in range(10):
            yield bytes([i % 256]) * TEST_PCM_FORMAT.pcm_sample_size
            await asyncio.sleep(0.1)

    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_slow_source(), source_name="test")
    await asyncio.sleep(0.25)
    await audio_buffer.clear()
    await asyncio.sleep(0.1)
    assert len(controller._active_sessions) == 0
    assert len(controller._queues) == 0
    assert len(controller._workers) == 0


@pytest.mark.asyncio
async def test_worker_cancelled_on_buffer_clear(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
) -> None:
    """Worker task is cancelled when buffer is cleared."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    session_key = "test_prov:track:test_123"
    worker = controller._workers.get(session_key)
    assert worker is not None
    await audio_buffer.clear()
    await asyncio.sleep(0.1)
    assert worker.cancelled() or worker.done()


# -- Edge cases --


@pytest.mark.asyncio
async def test_finalized_guard_prevents_double_finalize(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """Invoking the chunk callback with is_last_chunk=True twice only finalizes once."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    assert len(audio_buffer._chunk_callbacks) == 1
    cb = audio_buffer._chunk_callbacks[0]
    # First EOF
    cb(0, b"", True)
    # Second EOF
    cb(1, b"", True)
    await asyncio.sleep(0.3)
    mock_provider.finalize.assert_called_once()


@pytest.mark.asyncio
async def test_provider_error_during_chunk_processing(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """Provider raising in process_pcm_chunk still processes remaining chunks and finalizes."""
    call_count = 0

    async def _flaky_process(_session_id: str, _chunk: bytes) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("transient error")

    mock_provider.process_pcm_chunk = AsyncMock(side_effect=_flaky_process)
    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_make_source(3), source_name="test")
    await asyncio.sleep(0.3)
    # All 3 chunks were attempted
    assert call_count == 3
    mock_provider.finalize.assert_called_once()


@pytest.mark.asyncio
async def test_provider_error_during_start(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """One provider fails start_analysis, another succeeds. Successful one works."""
    prov_fail = _create_mock_provider(instance_id="prov_fail", name="FailProv")
    prov_fail.start_analysis.side_effect = RuntimeError("init failed")
    prov_ok = _create_mock_provider(instance_id="prov_ok", name="OkProv")
    mock_mass.get_providers.return_value = [prov_fail, prov_ok]
    mock_mass.get_provider = MagicMock(return_value=prov_ok)

    await controller.start_analysis(audio_buffer, mock_stream_details)
    audio_buffer.fill(_make_source(2), source_name="test")
    await asyncio.sleep(0.3)

    assert prov_ok.process_pcm_chunk.call_count == 2
    prov_ok.finalize.assert_called_once()
    prov_fail.process_pcm_chunk.assert_not_called()
    prov_fail.finalize.assert_not_called()


@pytest.mark.asyncio
async def test_version_check_skips_provider(
    controller: AudioAnalysisController,
    mock_mass: MagicMock,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """Controller skips provider when stored version >= provider version."""
    mock_provider.analysis_version = 1
    mock_provider.domain = "test_aa"
    mock_mass.get_providers.return_value = [mock_provider]
    mock_mass.music.get_audio_analysis_version = AsyncMock(return_value=1)

    audio_buffer = AudioBuffer(TEST_PCM_FORMAT)
    await controller.start_analysis(audio_buffer, mock_stream_details)

    mock_provider.start_analysis.assert_not_called()
    assert not controller._active_sessions


@pytest.mark.asyncio
async def test_finalize_cleans_up_provider_sessions() -> None:
    """Verify provider._sessions is cleaned up after finalize, even if _finalize raises."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider._sessions = {"test_session": MagicMock(spec=AnalysisSessionData)}
    provider._finalize = AsyncMock()

    await AudioAnalysisProvider.finalize(provider, "test_session")

    provider._finalize.assert_called_once_with("test_session")
    assert "test_session" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_cleans_up_sessions_on_error() -> None:
    """Verify provider._sessions is cleaned up even when _finalize raises."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider._sessions = {"test_session": MagicMock(spec=AnalysisSessionData)}
    provider._finalize = AsyncMock(side_effect=RuntimeError("analysis failed"))

    with pytest.raises(RuntimeError, match="analysis failed"):
        await AudioAnalysisProvider.finalize(provider, "test_session")

    assert "test_session" not in provider._sessions
