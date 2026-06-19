"""Tests for the AudioAnalysisController."""

from __future__ import annotations

import asyncio
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType
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
    prov.start_analysis = AsyncMock(return_value=True)
    prov.process_pcm_chunk = AsyncMock()
    prov.finalize = AsyncMock()
    prov.cancel = AsyncMock()
    return prov


async def _send_chunks(audio_buffer: AudioBuffer, num_chunks: int) -> None:
    """Deliver chunks and EOF directly via the registered callback."""
    cb = audio_buffer._chunk_callbacks[0]
    for i in range(num_chunks):
        await cb(i, ONE_SECOND_CHUNK, False)
    await cb(num_chunks, b"", True)


@pytest.fixture
def mock_provider() -> MagicMock:
    """Return a single mock AudioAnalysisProvider."""
    return _create_mock_provider()


@pytest.fixture
def mock_mass(mock_provider: MagicMock) -> MagicMock:
    """Return a mock MusicAssistant instance wired to mock_provider."""
    mass = MagicMock()
    mass._created_tasks = []

    def _track_create_task(coro):  # type: ignore[no-untyped-def]
        task = asyncio.ensure_future(coro)
        mass._created_tasks.append(task)
        return task

    mass.create_task = MagicMock(side_effect=_track_create_task)
    mass.get_providers = MagicMock(return_value=[mock_provider])
    mass.get_provider = MagicMock(return_value=mock_provider)
    return mass


async def _await_tasks(mock_mass: MagicMock) -> None:
    """Await all tasks created via mock_mass.create_task, including nested ones."""
    awaited = 0
    while awaited < len(mock_mass._created_tasks):
        pending = mock_mass._created_tasks[awaited:]
        awaited = len(mock_mass._created_tasks)
        await asyncio.gather(*pending, return_exceptions=True)


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
    mock_mass: MagicMock,
) -> None:
    """Provider receives all PCM chunks via the worker."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    await _send_chunks(audio_buffer, 3)
    await _await_tasks(mock_mass)
    assert mock_provider.process_pcm_chunk.call_count == 3


@pytest.mark.asyncio
async def test_finalize_called_on_eof(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """After EOF, provider.finalize is called with the session key."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    await _send_chunks(audio_buffer, 2)
    await _await_tasks(mock_mass)
    session_key = "test_prov://track/test_123"
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
    await _send_chunks(audio_buffer, 3)
    await _await_tasks(mock_mass)

    assert prov_a.process_pcm_chunk.call_count == 3
    assert prov_b.process_pcm_chunk.call_count == 3
    prov_a.finalize.assert_called_once()
    prov_b.finalize.assert_called_once()


@pytest.mark.asyncio
async def test_session_cleaned_up_after_finalize(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """Internal dicts are empty after finalize completes."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    await _send_chunks(audio_buffer, 1)
    await _await_tasks(mock_mass)
    assert len(controller._active_sessions) == 0
    assert len(controller._workers) == 0


# -- Cancel path --


@pytest.mark.asyncio
async def test_cancel_on_buffer_clear(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """Clearing the buffer triggers provider.cancel."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    cb = audio_buffer._chunk_callbacks[0]
    await cb(0, ONE_SECOND_CHUNK, False)
    await cb(1, ONE_SECOND_CHUNK, False)
    await audio_buffer.clear()
    await _await_tasks(mock_mass)
    session_key = "test_prov://track/test_123"
    mock_provider.cancel.assert_called_once_with(session_key)


@pytest.mark.asyncio
async def test_session_cleaned_up_after_cancel(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """Internal dicts are empty after cancel."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    cb = audio_buffer._chunk_callbacks[0]
    await cb(0, ONE_SECOND_CHUNK, False)
    await audio_buffer.clear()
    await _await_tasks(mock_mass)
    assert len(controller._active_sessions) == 0
    assert len(controller._workers) == 0


@pytest.mark.asyncio
async def test_worker_cancelled_on_buffer_clear(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """Worker task is cancelled when buffer is cleared."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    session_key = "test_prov://track/test_123"
    worker = controller._workers.get(session_key)
    assert worker is not None
    await audio_buffer.clear()
    await _await_tasks(mock_mass)
    assert worker.cancelled() or worker.done()


# -- Edge cases --


@pytest.mark.asyncio
async def test_finalized_guard_prevents_double_finalize(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """Invoking the chunk callback with is_last_chunk=True twice only finalizes once."""
    await controller.start_analysis(audio_buffer, mock_stream_details)
    assert len(audio_buffer._chunk_callbacks) == 1
    cb = audio_buffer._chunk_callbacks[0]
    await cb(0, b"", True)
    await cb(1, b"", True)
    await _await_tasks(mock_mass)
    mock_provider.finalize.assert_called_once()


@pytest.mark.asyncio
async def test_provider_error_during_chunk_processing_evicts_provider(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """Provider that raises in process_pcm_chunk is evicted from the session.

    The first chunk processes successfully. The second chunk's exception
    triggers eviction. The third chunk is not delivered. The provider's
    cancel hook is dispatched (replaces finalize for evicted providers).
    """
    call_count = 0

    async def _flaky_process(_session_id: str, _chunk: bytes) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("transient error")

    mock_provider.process_pcm_chunk = AsyncMock(side_effect=_flaky_process)
    await controller.start_analysis(audio_buffer, mock_stream_details)
    await _send_chunks(audio_buffer, 3)
    await _await_tasks(mock_mass)

    # Provider was called twice: chunk 1 (success), chunk 2 (raised → evicted)
    assert call_count == 2
    # Evicted provider does NOT get finalize, but does get cancel
    mock_provider.finalize.assert_not_called()
    mock_provider.cancel.assert_called_once()


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
    await _send_chunks(audio_buffer, 2)
    await _await_tasks(mock_mass)

    assert prov_ok.process_pcm_chunk.call_count == 2
    prov_ok.finalize.assert_called_once()
    prov_fail.process_pcm_chunk.assert_not_called()
    prov_fail.finalize.assert_not_called()


@pytest.mark.asyncio
async def test_slow_provider_removed_after_timeout(
    controller: AudioAnalysisController,
    audio_buffer: AudioBuffer,
    mock_stream_details: MagicMock,
    mock_mass: MagicMock,
) -> None:
    """A provider that exceeds the chunk timeout is removed from the session."""
    prov_slow = _create_mock_provider(instance_id="prov_slow", name="SlowProv")
    never_done = asyncio.Event()

    async def _hang(_session_id: str, _chunk: bytes) -> None:
        await never_done.wait()

    prov_slow.process_pcm_chunk = AsyncMock(side_effect=_hang)

    prov_fast = _create_mock_provider(instance_id="prov_fast", name="FastProv")
    mock_mass.get_providers.return_value = [prov_slow, prov_fast]

    def _get_prov(pid: str) -> MagicMock:
        return {"prov_slow": prov_slow, "prov_fast": prov_fast}[pid]

    mock_mass.get_provider = MagicMock(side_effect=_get_prov)

    with unittest.mock.patch(
        "music_assistant.controllers.streams.audio_analysis.REAL_TIME_PACE_INTERVAL_SECONDS_CEILING",
        0.1,
    ):
        await controller.start_analysis(audio_buffer, mock_stream_details)
        await _send_chunks(audio_buffer, 3)
        await _await_tasks(mock_mass)

    assert prov_fast.process_pcm_chunk.call_count == 3
    prov_fast.finalize.assert_called_once()
    assert prov_slow.process_pcm_chunk.call_count == 1


@pytest.mark.asyncio
async def test_provider_rejects_analysis(
    controller: AudioAnalysisController,
    mock_mass: MagicMock,
    mock_stream_details: MagicMock,
    mock_provider: MagicMock,
) -> None:
    """Controller skips provider when start_analysis returns False."""
    mock_provider.start_analysis = AsyncMock(return_value=False)
    mock_mass.get_providers.return_value = [mock_provider]

    audio_buffer = AudioBuffer(TEST_PCM_FORMAT)
    await controller.start_analysis(audio_buffer, mock_stream_details)

    assert not controller._active_sessions


@pytest.mark.asyncio
async def test_finalize_cleans_up_provider_sessions() -> None:
    """Verify provider._sessions is cleaned up after finalize."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.logger = MagicMock()
    provider._sessions = {"test_session": MagicMock(spec=AnalysisSessionData)}
    provider._finalize = AsyncMock(return_value=None)

    await AudioAnalysisProvider.finalize(provider, "test_session")

    provider._finalize.assert_called_once_with("test_session")
    assert "test_session" not in provider._sessions


@pytest.mark.asyncio
async def test_provider_start_analysis_uses_media_type_for_version_gating() -> None:
    """Version gating must include media_type so analyses do not collide across item types."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.mass = MagicMock()
    provider.mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    provider._sessions = {}
    provider._start_analysis = AsyncMock(return_value=True)
    provider.domain = "test_domain"
    provider.analysis_version = 1

    streamdetails = MagicMock()
    streamdetails.item_id = "shared_id"
    streamdetails.provider = "test_prov"
    streamdetails.media_type = MediaType.RADIO

    accepted = await AudioAnalysisProvider.start_analysis(
        provider,
        session_id="session_1",
        streamdetails=streamdetails,
        audio_format=TEST_PCM_FORMAT,
    )

    assert accepted is True
    provider.mass.streams.audio_analysis.get_audio_analysis_version.assert_awaited_once_with(
        "shared_id",
        "test_prov",
        "test_domain",
        media_type=MediaType.RADIO,
    )


@pytest.mark.asyncio
async def test_finalize_swallows_finalize_exception_and_cleans_up() -> None:
    """Verify provider._sessions is cleaned up even when _finalize raises.

    The finalize wrapper catches _finalize exceptions and logs ERROR; it must
    not propagate them to the controller, and must still pop the session.
    """
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.logger = MagicMock()
    provider._sessions = {"test_session": MagicMock(spec=AnalysisSessionData)}
    provider._finalize = AsyncMock(side_effect=RuntimeError("analysis failed"))

    # MUST NOT raise — exception is swallowed and logged
    await AudioAnalysisProvider.finalize(provider, "test_session")

    assert "test_session" not in provider._sessions
    provider.logger.error.assert_called_once()
