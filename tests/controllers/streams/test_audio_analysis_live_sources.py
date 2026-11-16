"""Tests for analysis of live sources and the live metadata analysis providers report."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat, AudioSource
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_analysis import (
    LIVE_SOURCE_BACKLOG_SECONDS,
    AudioAnalysisController,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.providers.acoustid_lookup.provider import AcoustidLookupProvider
from music_assistant.providers.loudness_analysis.provider import LoudnessAnalysisProvider
from music_assistant.providers.smart_fades.provider import SmartFadesProvider
from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
)
ONE_SECOND = PCM_FORMAT.pcm_sample_size
PLAYER_ID = "living_room"
SOURCE_PROVIDER_ID = "sendspin_source--abc"
RADIO_ID = "tunein--abc"


class _RecognizingProvider(AudioAnalysisProvider):
    """Analysis provider that follows radio and live sources and has a result ready."""

    media_types = frozenset({MediaType.RADIO, MediaType.AUDIO_SOURCE})

    async def _start_analysis(
        self, session_id: str, streamdetails: StreamDetails, audio_format: AudioFormat
    ) -> bool:
        return True

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        return None

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        return AudioAnalysisData(loudness_integrated=-14.0)


def _aa_provider(instance_id: str, media_types: frozenset[MediaType]) -> MagicMock:
    prov = MagicMock(spec=AudioAnalysisProvider)
    prov.instance_id = instance_id
    prov.name = instance_id
    prov.domain = instance_id
    prov.available = True
    prov.media_types = media_types
    prov.start_analysis = AsyncMock(return_value=True)
    prov.process_pcm_chunk = AsyncMock()
    prov.finalize = AsyncMock()
    prov.cancel = AsyncMock()
    return prov


def _source_session(*, allow_audio_analysis: bool) -> AudioSourceSession:
    source = AudioSource(
        item_id="line_in",
        provider=SOURCE_PROVIDER_ID,
        name="Turntable",
        provider_mappings=set(),
        allow_audio_analysis=allow_audio_analysis,
    )
    return AudioSourceSession(
        player_id=PLAYER_ID, source=source, provider_instance_id=SOURCE_PROVIDER_ID
    )


def _source_streamdetails() -> StreamDetails:
    return StreamDetails(
        provider=SOURCE_PROVIDER_ID,
        item_id="line_in",
        audio_format=PCM_FORMAT,
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.CUSTOM,
    )


def _radio_streamdetails() -> StreamDetails:
    return StreamDetails(
        provider=RADIO_ID,
        item_id="station",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.RADIO,
        stream_type=StreamType.HTTP,
        path="https://example.com/station.mp3",
        queue_id=PLAYER_ID,
    )


@pytest.fixture
def registry() -> dict[str, Any]:
    """Return the providers mass resolves by instance id, a test adds its analysis providers."""
    return {}


@pytest.fixture
async def controller(registry: dict[str, Any]) -> AsyncIterator[AudioAnalysisController]:
    """Return a controller that runs its workers and stores into mocks."""
    streams = MagicMock()
    mass = streams.mass
    mass.create_task = MagicMock(
        side_effect=lambda coro: asyncio.get_running_loop().create_task(coro)
    )
    mass.get_provider = MagicMock(side_effect=lambda pid, **_kw: registry.get(pid))
    mass.get_providers = MagicMock(side_effect=lambda _type: list(registry.values()))
    controller = AudioAnalysisController(streams)
    controller._database_ready = True
    mass.streams.audio_analysis = controller
    controller.get_audio_analysis_version = AsyncMock(return_value=None)  # type: ignore[method-assign]
    controller.set_audio_analysis = AsyncMock()  # type: ignore[method-assign]
    controller.record_analysis_failure = AsyncMock()  # type: ignore[method-assign]
    yield controller
    workers = list(controller._workers.values())
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


def _recognizer(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> _RecognizingProvider:
    manifest = MagicMock()
    manifest.domain = "recognizer"
    config = MagicMock()
    config.instance_id = "recognizer"
    config.get_value = MagicMock(return_value="GLOBAL")
    provider = _RecognizingProvider(controller.mass, manifest, config, supported_features=set())
    provider.available = True
    registry[provider.instance_id] = provider
    return provider


def _mass(controller: AudioAnalysisController) -> MagicMock:
    return cast("MagicMock", controller.mass)


async def _until(condition: Any) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0)


async def _start_source(
    controller: AudioAnalysisController, source_session: AudioSourceSession
) -> str:
    """Start a live source session and wait until its providers took it."""
    _mass(controller).players.get_audio_source_session = MagicMock(return_value=source_session)
    session_key = controller.start_source_analysis(
        source_session, _source_streamdetails(), PCM_FORMAT
    )
    assert session_key is not None
    await _until(lambda: session_key in controller._active_sessions)
    return session_key


@pytest.mark.asyncio
async def test_provider_that_declared_it_follows_a_source_that_allows_analysis(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A provider for live sources gets the audio of a source that allows analysis."""
    prov = _aa_provider("follower", frozenset({MediaType.AUDIO_SOURCE}))
    registry["follower"] = prov

    session_key = await _start_source(controller, _source_session(allow_audio_analysis=True))
    controller.feed_source_analysis(session_key, b"\x00" * (3 * ONE_SECOND))
    await _until(lambda: prov.process_pcm_chunk.await_count == 3)

    assert all(len(call.args[1]) == ONE_SECOND for call in prov.process_pcm_chunk.await_args_list)


@pytest.mark.asyncio
async def test_source_that_does_not_allow_analysis_is_offered_to_no_provider(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A source without the flag, such as a streaming receiver, reaches no provider."""
    prov = _aa_provider("follower", frozenset({MediaType.AUDIO_SOURCE}))
    registry["follower"] = prov

    session_key = controller.start_source_analysis(
        _source_session(allow_audio_analysis=False), _source_streamdetails(), PCM_FORMAT
    )

    assert session_key is None
    prov.start_analysis.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_that_did_not_declare_live_sources_gets_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A provider that did not declare live sources gets none, even one that allows it."""
    prov = _aa_provider("tracks_only", AudioAnalysisProvider.media_types)
    registry["tracks_only"] = prov

    session_key = controller.start_source_analysis(
        _source_session(allow_audio_analysis=True), _source_streamdetails(), PCM_FORMAT
    )

    assert session_key is None
    prov.start_analysis.assert_not_awaited()


@pytest.mark.parametrize(
    "provider_cls",
    [
        LoudnessAnalysisProvider,
        SmartFadesProvider,
        SonicAnalysisProvider,
        AcoustidLookupProvider,
    ],
)
@pytest.mark.asyncio
async def test_builtin_provider_is_not_started_for_a_live_source(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    provider_cls: type[AudioAnalysisProvider],
) -> None:
    """No analysis that ships with the server, loudness included, measures a live source."""
    prov = _aa_provider("builtin", provider_cls.media_types)
    registry["builtin"] = prov

    session_key = controller.start_source_analysis(
        _source_session(allow_audio_analysis=True), _source_streamdetails(), PCM_FORMAT
    )

    assert session_key is None
    prov.start_analysis.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_provider_never_holds_up_the_source(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source streams at its own pace while a stuck provider gets only a bounded backlog."""
    stuck = asyncio.Event()
    prov = _aa_provider("slow", frozenset({MediaType.AUDIO_SOURCE}))

    async def _stuck(*_args: Any) -> None:
        await stuck.wait()

    prov.process_pcm_chunk = AsyncMock(side_effect=_stuck)
    registry["slow"] = prov
    source_session = _source_session(allow_audio_analysis=True)
    _mass(controller).players.get_audio_source_session = MagicMock(return_value=source_session)
    audio_mass = MagicMock()
    audio_mass.streams.audio_analysis = controller
    audio = StreamsAudio(audio_mass)
    chunk = b"\x00" * (ONE_SECOND // 50)
    chunk_count = 50 * 30
    providers_started = asyncio.Event()

    def _start(**_kw: Any) -> bool:
        providers_started.set()
        return True

    prov.start_analysis.side_effect = _start

    async def _source_pcm(*_args: Any) -> AsyncGenerator[bytes]:
        # the providers take the session first, so every second below goes to them
        await providers_started.wait()
        for _ in range(chunk_count):
            # a real source paces itself, which is when the analysis worker gets its turn
            await asyncio.sleep(0)
            yield chunk

    monkeypatch.setattr(audio, "_iter_audio_source_pcm", _source_pcm)

    received = 0
    async with asyncio.timeout(2):
        async for pcm in audio.get_audio_source_stream(
            _source_streamdetails(), PCM_FORMAT, source_session=source_session
        ):
            received += len(pcm)
            if received == len(chunk) * chunk_count // 2:
                live = next(iter(controller._live_sources.values()))
                # one second sits with the provider, the backlog is full and the rest dropped
                assert live.blocks.qsize() == LIVE_SOURCE_BACKLOG_SECONDS

    assert received == len(chunk) * chunk_count
    assert prov.process_pcm_chunk.await_count == 1
    await _until(lambda: prov.cancel.await_count == 1)
    prov.finalize.assert_not_awaited()
    assert not controller._live_sources
    assert not controller._active_sessions
    assert not controller._workers


@pytest.mark.asyncio
async def test_live_source_session_stores_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Neither a result nor a failure of a live source session is stored."""
    provider = _recognizer(controller, registry)
    failing = await _start_source(controller, _source_session(allow_audio_analysis=True))
    finishing = await _start_source(controller, _source_session(allow_audio_analysis=True))

    await provider.abort(failing, "audio processing failed (boom)")
    await provider.finalize(finishing)

    assert not provider._sessions
    controller.set_audio_analysis.assert_not_awaited()  # type: ignore[attr-defined]
    controller.record_analysis_failure.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stream_that_ends_while_providers_start_leaves_no_session(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A provider that took the session after its stream already ended is cancelled."""
    prov = _aa_provider("follower", frozenset({MediaType.AUDIO_SOURCE}))
    registry["follower"] = prov
    source_session = _source_session(allow_audio_analysis=True)

    session_key = controller.start_source_analysis(
        source_session, _source_streamdetails(), PCM_FORMAT
    )
    assert session_key is not None
    controller.stop_source_analysis(session_key)
    await _until(lambda: prov.cancel.await_count == 1)

    prov.start_analysis.assert_awaited_once()
    assert not controller._active_sessions
    assert not controller._workers


@pytest.mark.asyncio
async def test_reported_metadata_shows_on_a_live_source_until_its_plugin_reports(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A recognised song shows while the plugin is silent and yields once it reports."""
    provider = _recognizer(controller, registry)
    source_session = _source_session(allow_audio_analysis=True)
    session_key = await _start_source(controller, source_session)
    recognised = StreamMetadata(title="Recognised", artist="Someone")

    provider.report_stream_metadata(session_key, recognised)

    assert source_session.stream_metadata is recognised
    assert source_session.stream_metadata_reported is False
    _mass(controller).players.trigger_player_update.assert_called_with(PLAYER_ID)

    owners = StreamMetadata(title="From the plugin")
    source_session.stream_metadata = owners
    source_session.stream_metadata_reported = True
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised later"))

    assert source_session.stream_metadata is owners


@pytest.mark.asyncio
async def test_reported_metadata_shows_on_a_radio_stream_until_the_stream_reports(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A recognised song shows while the station sends none and yields to the station's own."""
    provider = _recognizer(controller, registry)
    streamdetails = _radio_streamdetails()
    await controller.start_analysis(AudioBuffer(PCM_FORMAT), streamdetails)
    session_key = streamdetails.uri
    assert session_key in controller._active_sessions
    recognised = StreamMetadata(title="Recognised", artist="Someone")

    provider.report_stream_metadata(session_key, recognised)
    assert streamdetails.stream_metadata is recognised
    _mass(controller).player_queues.signal_update.assert_called_with(PLAYER_ID)
    newer = StreamMetadata(title="Recognised next")
    provider.report_stream_metadata(session_key, newer)
    assert streamdetails.stream_metadata is newer

    stations = StreamMetadata(title="From the station")
    streamdetails.stream_metadata = stations
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised later"))

    assert streamdetails.stream_metadata is stations


@pytest.mark.asyncio
async def test_reported_metadata_never_overrides_what_a_station_sends_from_the_start(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A station that sends its own metadata keeps it from the first report on."""
    provider = _recognizer(controller, registry)
    streamdetails = _radio_streamdetails()
    stations = StreamMetadata(title="From the station")
    streamdetails.stream_metadata = stations
    await controller.start_analysis(AudioBuffer(PCM_FORMAT), streamdetails)
    assert streamdetails.uri in controller._active_sessions

    provider.report_stream_metadata(streamdetails.uri, StreamMetadata(title="Recognised"))

    assert streamdetails.stream_metadata is stations


@pytest.mark.asyncio
async def test_metadata_reported_for_a_track_is_ignored(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Live metadata is about a stream: a track session shows nothing of it."""
    provider = _recognizer(controller, registry)
    provider.media_types = frozenset({MediaType.TRACK})
    streamdetails = StreamDetails(
        provider=RADIO_ID,
        item_id="track",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="https://example.com/track.mp3",
        queue_id=PLAYER_ID,
    )
    await controller.start_analysis(AudioBuffer(PCM_FORMAT), streamdetails)
    assert streamdetails.uri in controller._active_sessions

    provider.report_stream_metadata(streamdetails.uri, StreamMetadata(title="Recognised"))

    assert streamdetails.stream_metadata is None
    _mass(controller).player_queues.signal_update.assert_not_called()
