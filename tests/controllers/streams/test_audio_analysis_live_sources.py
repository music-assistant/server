"""Tests for analysis of live sources and the live metadata analysis providers report."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat, AudioSource
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.players.audio_sources import (
    AudioSourceSession,
    PlayerAudioSourcesMixin,
)
from music_assistant.controllers.players.state_updates import PLAYER_UPDATE_DEBOUNCE
from music_assistant.controllers.streams import audio_analysis as audio_analysis_module
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_analysis import (
    LIVE_SOURCE_BACKLOG_SECONDS,
    AudioAnalysisController,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.constants import BufferMode
from music_assistant.controllers.streams.controller import AudioSourceStream
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
LIVE_ONLY = frozenset({MediaType.AUDIO_SOURCE})
STREAM_TOKEN = "stream-1"


class _RecognizingProvider(AudioAnalysisProvider):
    """Analysis provider that follows live sources, whose start can be held open."""

    media_types = frozenset({MediaType.RADIO, MediaType.AUDIO_SOURCE})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.start_gate: asyncio.Event | None = None
        self.starting = asyncio.Event()
        self.cancel_gate: asyncio.Event | None = None
        self.cancelling = asyncio.Event()

    async def _start_analysis(
        self, session_id: str, streamdetails: StreamDetails, audio_format: AudioFormat
    ) -> bool:
        self.starting.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        return True

    async def cancel(self, session_id: str) -> None:
        self.cancelling.set()
        if self.cancel_gate is not None:
            # releasing a session can take a moment, as closing a decoder does
            await self.cancel_gate.wait()
        await super().cancel(session_id)

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        return None

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        return AudioAnalysisData(loudness_integrated=-14.0)


class _Players(PlayerAudioSourcesMixin):
    """The live source sessions of the player controller, with nothing else of it."""

    def __init__(self) -> None:
        self._source_sessions: dict[str, AudioSourceSession] = {}
        self.mass = MagicMock()
        self.logger = MagicMock()
        self.updated_players: list[str] = []

    def trigger_player_update(
        self,
        player_id: str,
        force_update: bool = False,
        debounce_delay: float = PLAYER_UPDATE_DEBOUNCE,
    ) -> None:
        """Record that a player update was signalled."""
        self.updated_players.append(player_id)


def _aa_provider(instance_id: str, media_types: frozenset[MediaType]) -> MagicMock:
    prov = MagicMock(spec=AudioAnalysisProvider)
    prov.instance_id = instance_id
    prov.name = instance_id
    prov.domain = instance_id
    prov.available = True
    prov.unloading = False
    prov.media_types = media_types
    prov.start_analysis = AsyncMock(return_value=True)
    prov.process_pcm_chunk = AsyncMock()
    prov.finalize = AsyncMock()
    prov.cancel = AsyncMock()
    return prov


def _audio_source(*, allow_audio_analysis: bool) -> AudioSource:
    return AudioSource(
        item_id="line_in",
        provider=SOURCE_PROVIDER_ID,
        name="Turntable",
        provider_mappings=set(),
        allow_audio_analysis=allow_audio_analysis,
    )


def _source_streamdetails(stream_metadata: StreamMetadata | None = None) -> StreamDetails:
    return StreamDetails(
        provider=SOURCE_PROVIDER_ID,
        item_id="line_in",
        audio_format=PCM_FORMAT,
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.CUSTOM,
        stream_metadata=stream_metadata,
    )


@pytest.fixture
def registry() -> dict[str, Any]:
    """Return the providers mass resolves by instance id, a test adds its analysis providers."""
    return {}


@pytest.fixture
async def controller(registry: dict[str, Any]) -> AsyncIterator[AudioAnalysisController]:
    """Return a controller that starts its tasks eagerly, as production does."""
    streams = MagicMock()
    mass = streams.mass
    mass.create_task = MagicMock(
        side_effect=lambda coro: asyncio.Task(
            coro, loop=asyncio.get_running_loop(), eager_start=True
        )
    )
    mass.get_provider = MagicMock(side_effect=lambda pid, **_kw: registry.get(pid))
    mass.get_providers = MagicMock(side_effect=lambda _type: list(registry.values()))
    mass.players = _Players()
    controller = AudioAnalysisController(streams)
    controller._database_ready = True
    mass.streams.audio_analysis = controller
    controller.get_audio_analysis_version = AsyncMock(return_value=None)  # type: ignore[method-assign]
    controller.set_audio_analysis = AsyncMock()  # type: ignore[method-assign]
    controller.record_analysis_failure = AsyncMock()  # type: ignore[method-assign]
    controller.close_database = AsyncMock()  # type: ignore[method-assign]
    yield controller
    await controller.close()


def _players(controller: AudioAnalysisController) -> _Players:
    return cast("_Players", controller.mass.players)


def _source_session(
    controller: AudioAnalysisController, *, allow_audio_analysis: bool = True
) -> AudioSourceSession:
    """Return a source session that is live on its player, its stream claimed."""
    session = AudioSourceSession(
        player_id=PLAYER_ID,
        source=_audio_source(allow_audio_analysis=allow_audio_analysis),
        provider_instance_id=SOURCE_PROVIDER_ID,
    )
    session.stream_session_id = STREAM_TOKEN
    _players(controller)._source_sessions[PLAYER_ID] = session
    return session


def _recognizer(
    controller: AudioAnalysisController, registry: dict[str, Any], instance_id: str = "recognizer"
) -> _RecognizingProvider:
    manifest = MagicMock()
    manifest.domain = instance_id
    config = MagicMock()
    config.instance_id = instance_id
    config.get_value = MagicMock(return_value="GLOBAL")
    provider = _RecognizingProvider(controller.mass, manifest, config, supported_features=set())
    provider.available = True
    registry[provider.instance_id] = provider
    return provider


async def _unload(provider: AudioAnalysisProvider) -> None:
    """Unload a provider the way the server does, marked as unloading first."""
    provider.unloading = True
    await provider.unload()


async def _until(condition: Any) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0)


def _start_analysis(
    controller: AudioAnalysisController, source_session: AudioSourceSession
) -> str | None:
    """Start the analysis of a source session for the stream that claimed it."""
    assert source_session.stream_session_id is not None
    return controller.start_source_analysis(
        source_session, source_session.stream_session_id, _source_streamdetails(), PCM_FORMAT
    )


async def _start_source(
    controller: AudioAnalysisController, source_session: AudioSourceSession
) -> str:
    """Start a live source session whose providers all start without waiting."""
    session_key = _start_analysis(controller, source_session)
    assert session_key is not None
    # the worker starts eagerly, so it already waits for audio here
    assert controller._active_sessions[session_key]
    return session_key


async def _ended(worker: asyncio.Task[None]) -> None:
    async with asyncio.timeout(2):
        await asyncio.gather(worker, return_exceptions=True)


async def _stop_source(controller: AudioAnalysisController, session_key: str) -> None:
    """Stop a live source session and wait until its worker released it."""
    worker = controller._workers.get(session_key)
    controller.stop_source_analysis(session_key)
    if worker is not None:
        await _ended(worker)


def _assert_nothing_left(
    controller: AudioAnalysisController, *providers: AudioAnalysisProvider
) -> None:
    assert not controller._live_sources
    assert not controller._workers
    assert not controller._active_sessions
    for provider in providers:
        assert not provider._sessions


def _audio(controller: AudioAnalysisController) -> StreamsAudio:
    audio_mass = MagicMock()
    audio_mass.streams.audio_analysis = controller
    return StreamsAudio(audio_mass)


@pytest.mark.asyncio
async def test_provider_that_declared_it_follows_a_source_that_allows_analysis(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A provider for live sources gets the audio of a source that allows analysis."""
    prov = _aa_provider("follower", LIVE_ONLY)
    registry["follower"] = prov

    session_key = await _start_source(controller, _source_session(controller))
    controller.feed_source_analysis(session_key, b"\x00" * (3 * ONE_SECOND))
    await _until(lambda: prov.process_pcm_chunk.await_count == 3)

    assert all(len(call.args[1]) == ONE_SECOND for call in prov.process_pcm_chunk.await_args_list)


@pytest.mark.asyncio
async def test_source_that_does_not_allow_analysis_is_offered_to_no_provider(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A source without the flag, such as a streaming receiver, reaches no provider."""
    prov = _aa_provider("follower", LIVE_ONLY)
    registry["follower"] = prov

    session_key = _start_analysis(
        controller, _source_session(controller, allow_audio_analysis=False)
    )

    assert session_key is None
    prov.start_analysis.assert_not_awaited()


async def test_analysis_ends_when_the_source_stops_allowing_it(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Analysis the user disables for a source that plays ends, and does not start again."""
    follower = _aa_provider("follower", LIVE_ONLY)
    registry["follower"] = follower
    recognizer = _recognizer(controller, registry)
    source_session = _source_session(controller)
    placeholder = StreamMetadata(title="Turntable")
    source_session.attach_streamdetails(_source_streamdetails(placeholder))
    session_key = await _start_source(controller, source_session)
    worker = controller._workers[session_key]
    recognizer.report_stream_metadata(session_key, StreamMetadata(title="Recognised"))

    _players(controller).refresh_source(PLAYER_ID, _audio_source(allow_audio_analysis=False))
    controller.feed_source_analysis(session_key, b"\x00" * ONE_SECOND)
    await _ended(worker)

    follower.process_pcm_chunk.assert_not_awaited()
    follower.cancel.assert_awaited_once_with(session_key)
    assert source_session.stream_metadata is placeholder
    _assert_nothing_left(controller, recognizer)
    # a resume of the same selection asks again
    assert _start_analysis(controller, source_session) is None


@pytest.mark.asyncio
async def test_provider_that_did_not_declare_live_sources_gets_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A provider that did not declare live sources gets none, even one that allows it."""
    prov = _aa_provider("tracks_only", AudioAnalysisProvider.media_types)
    registry["tracks_only"] = prov

    session_key = _start_analysis(controller, _source_session(controller))

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

    session_key = _start_analysis(controller, _source_session(controller))

    assert session_key is None
    prov.start_analysis.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_source_no_provider_follows_costs_nothing(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a provider for live sources the stream starts no worker and copies no audio."""
    registry["loudness"] = _aa_provider("loudness", LoudnessAnalysisProvider.media_types)
    audio = _audio(controller)
    feed = MagicMock(wraps=controller.feed_source_analysis)
    monkeypatch.setattr(controller, "feed_source_analysis", feed)
    create_task = cast("MagicMock", controller.mass.create_task)

    async def _source_pcm(*_args: Any) -> AsyncGenerator[bytes]:
        for _ in range(5):
            yield b"\x00" * ONE_SECOND

    monkeypatch.setattr(audio, "_iter_audio_source_pcm", _source_pcm)

    received = [
        chunk
        async for chunk in audio.get_audio_source_stream(
            _source_streamdetails(),
            PCM_FORMAT,
            source_session=_source_session(controller),
            stream_session_id=STREAM_TOKEN,
        )
    ]

    assert len(received) == 5
    feed.assert_not_called()
    create_task.assert_not_called()
    _assert_nothing_left(controller)


@pytest.mark.asyncio
async def test_slow_provider_never_holds_up_the_source(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source streams at its own pace while a stuck provider gets only a bounded backlog."""
    stuck = asyncio.Event()
    prov = _aa_provider("slow", LIVE_ONLY)

    async def _stuck(*_args: Any) -> None:
        await stuck.wait()

    prov.process_pcm_chunk = AsyncMock(side_effect=_stuck)
    registry["slow"] = prov
    source_session = _source_session(controller)
    audio = _audio(controller)
    chunk = b"\x00" * (ONE_SECOND // 50)
    chunk_count = 50 * 30

    async def _source_pcm(*_args: Any) -> AsyncGenerator[bytes]:
        for _ in range(chunk_count):
            # a real source paces itself, which is when the analysis worker gets its turn
            await asyncio.sleep(0)
            yield chunk

    monkeypatch.setattr(audio, "_iter_audio_source_pcm", _source_pcm)

    received = 0
    async with asyncio.timeout(2):
        async for pcm in audio.get_audio_source_stream(
            _source_streamdetails(),
            PCM_FORMAT,
            source_session=source_session,
            stream_session_id=STREAM_TOKEN,
        ):
            received += len(pcm)
            if received == len(chunk) * chunk_count // 2:
                live = next(iter(controller._live_sources.values()))
                # one second sits with the provider, the backlog is full and the rest dropped
                assert live.blocks.qsize() == LIVE_SOURCE_BACKLOG_SECONDS

    assert received == len(chunk) * chunk_count
    assert prov.process_pcm_chunk.await_count == 1
    await _until(lambda: not controller._workers)
    prov.cancel.assert_awaited_once()
    prov.finalize.assert_not_awaited()
    _assert_nothing_left(controller)


@pytest.mark.parametrize("failing", ["start", "feed", "stop"])
@pytest.mark.asyncio
async def test_a_failing_analysis_tap_never_costs_playback(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
) -> None:
    """A tap that raises is switched off for the stream, and every chunk still plays."""
    registry["follower"] = _aa_provider("follower", LIVE_ONLY)
    audio = _audio(controller)
    calls = {"feed": 0, "stop": 0}
    start = controller.start_source_analysis
    feed = controller.feed_source_analysis
    stop = controller.stop_source_analysis

    def _start(*args: Any) -> str | None:
        if failing == "start":
            raise RuntimeError("boom")
        return start(*args)

    def _feed(*args: Any) -> None:
        calls["feed"] += 1
        if failing == "feed":
            raise RuntimeError("boom")
        feed(*args)

    def _stop(*args: Any) -> None:
        calls["stop"] += 1
        stop(*args)
        if failing == "stop":
            raise RuntimeError("boom")

    monkeypatch.setattr(controller, "start_source_analysis", _start)
    monkeypatch.setattr(controller, "feed_source_analysis", _feed)
    monkeypatch.setattr(controller, "stop_source_analysis", _stop)

    async def _source_pcm(*_args: Any) -> AsyncGenerator[bytes]:
        for index in range(5):
            yield bytes([index]) * ONE_SECOND

    monkeypatch.setattr(audio, "_iter_audio_source_pcm", _source_pcm)

    streamdetails = _source_streamdetails()
    with caplog.at_level(logging.WARNING):
        received = [
            chunk
            async for chunk in audio.get_audio_source_stream(
                streamdetails,
                PCM_FORMAT,
                source_session=_source_session(controller),
                stream_session_id=STREAM_TOKEN,
            )
        ]

    assert received == [bytes([index]) * ONE_SECOND for index in range(5)]
    assert not streamdetails.stream_error
    assert len([r for r in caplog.records if "plays on without it" in r.message]) == 1
    assert (
        calls
        == {
            "start": {"feed": 0, "stop": 0},
            "feed": {"feed": 1, "stop": 1},
            "stop": {"feed": 5, "stop": 1},
        }[failing]
    )
    await _until(lambda: not controller._workers)
    _assert_nothing_left(controller)


@pytest.mark.asyncio
async def test_live_source_session_stores_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Neither a result nor a failure of a live source session is stored."""
    provider = _recognizer(controller, registry)
    failing = await _start_source(controller, _source_session(controller))
    finishing = await _start_source(controller, _source_session(controller))

    await provider.abort(failing, "audio processing failed (boom)")
    await provider.finalize(finishing)

    assert not provider._sessions
    controller.set_audio_analysis.assert_not_awaited()  # type: ignore[attr-defined]
    controller.record_analysis_failure.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_source_that_stops_while_a_provider_starts_leaves_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A stop during the providers' start ends the worker and the partly started session."""
    provider = _recognizer(controller, registry)
    provider.start_gate = asyncio.Event()

    session_key = _start_analysis(controller, _source_session(controller))
    assert session_key is not None
    await _until(provider.starting.is_set)
    assert session_key in provider._sessions
    worker = controller._workers[session_key]

    controller.stop_source_analysis(session_key)
    await _ended(worker)

    assert worker.cancelled()
    _assert_nothing_left(controller, provider)


@pytest.mark.asyncio
async def test_cancelled_start_releases_everything(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A worker cancelled from outside during the start releases it all, and a stop after it is a no-op."""
    accepted = _recognizer(controller, registry, "accepted")
    starting = _recognizer(controller, registry, "starting")
    starting.start_gate = asyncio.Event()

    session_key = _start_analysis(controller, _source_session(controller))
    assert session_key is not None
    await _until(starting.starting.is_set)
    assert session_key in accepted._sessions
    worker = controller._workers[session_key]

    worker.cancel()
    await _ended(worker)
    controller.stop_source_analysis(session_key)

    _assert_nothing_left(controller, accepted, starting)


@pytest.mark.asyncio
async def test_close_during_the_start_leaves_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Shutting down while a provider starts releases the live source session completely."""
    accepted = _recognizer(controller, registry, "accepted")
    starting = _recognizer(controller, registry, "starting")
    running = await _start_source(controller, _source_session(controller))
    starting.start_gate = asyncio.Event()
    starting.starting.clear()
    session_key = _start_analysis(controller, _source_session(controller))
    assert session_key is not None
    await _until(starting.starting.is_set)
    assert running in accepted._sessions
    assert session_key in accepted._sessions

    await controller.close()

    _assert_nothing_left(controller, accepted, starting)


@pytest.mark.asyncio
async def test_close_lets_a_stopped_source_finish_releasing_its_session(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A source that stopped just before shutdown still releases its providers completely."""
    provider = _recognizer(controller, registry)
    session_key = await _start_source(controller, _source_session(controller))
    provider.cancel_gate = gate = asyncio.Event()
    try:
        controller.stop_source_analysis(session_key)
        await _until(provider.cancelling.is_set)

        closing = asyncio.create_task(controller.close())
        await asyncio.sleep(0)
    finally:
        gate.set()
    async with asyncio.timeout(2):
        await closing

    _assert_nothing_left(controller, provider)


@pytest.mark.asyncio
async def test_last_provider_leaving_while_the_worker_waits_leaves_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """The last provider unloads while the worker waits for audio, then the source stops."""
    provider = _recognizer(controller, registry)
    session_key = await _start_source(controller, _source_session(controller))
    worker = controller._workers[session_key]

    await _unload(provider)
    assert not worker.done()
    controller.stop_source_analysis(session_key)
    await _ended(worker)

    _assert_nothing_left(controller, provider)


@pytest.mark.asyncio
async def test_every_provider_declining_or_failing_at_once_registers_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A worker that ends during its eager start is neither returned nor kept."""
    declining = _aa_provider("declining", LIVE_ONLY)
    declining.start_analysis = AsyncMock(return_value=False)
    failing = _aa_provider("failing", LIVE_ONLY)
    failing.start_analysis = AsyncMock(side_effect=RuntimeError("boom"))
    registry.update(declining=declining, failing=failing)

    session_key = _start_analysis(controller, _source_session(controller))

    assert session_key is None
    _assert_nothing_left(controller)
    declining.cancel.assert_awaited_once()
    failing.cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_reported_metadata_shows_on_a_live_source_until_its_plugin_reports(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A recognised song shows while the plugin is silent and yields once it reports."""
    provider = _recognizer(controller, registry)
    source_session = _source_session(controller)
    session_key = await _start_source(controller, source_session)
    recognised = StreamMetadata(title="Recognised", artist="Someone")

    provider.report_stream_metadata(session_key, recognised)

    assert source_session.stream_metadata is recognised
    assert source_session.stream_metadata_reported is False
    assert _players(controller).updated_players == [PLAYER_ID]

    owners = StreamMetadata(title="From the plugin")
    _players(controller).update_source_metadata(
        PLAYER_ID, source_session.source_id, SOURCE_PROVIDER_ID, owners
    )
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised later"))

    assert source_session.stream_metadata is owners


@pytest.mark.parametrize("ending", ["stream_ends", "provider_unloads"])
@pytest.mark.asyncio
async def test_reported_metadata_is_withdrawn_when_its_analysis_ends(
    controller: AudioAnalysisController, registry: dict[str, Any], ending: str
) -> None:
    """The source falls back to its own metadata once the analysis that reported a song ends."""
    provider = _recognizer(controller, registry)
    source_session = _source_session(controller)
    placeholder = StreamMetadata(title="Turntable")
    source_session.attach_streamdetails(_source_streamdetails(placeholder))
    session_key = await _start_source(controller, source_session)
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised"))
    assert source_session.stream_metadata is not placeholder

    if ending == "stream_ends":
        await _stop_source(controller, session_key)
    else:
        await _unload(provider)
        # the worker withdraws it at its next step: the next second of audio
        controller.feed_source_analysis(session_key, b"\x00" * ONE_SECOND)
        await _until(lambda: source_session.stream_metadata is placeholder)

    assert source_session.stream_metadata is placeholder
    assert _players(controller).updated_players == [PLAYER_ID, PLAYER_ID]


@pytest.mark.asyncio
async def test_reported_metadata_is_withdrawn_when_its_provider_is_evicted(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A reporting provider that fails on the audio takes its report along, the session goes on."""
    provider = _recognizer(controller, registry)
    other = _aa_provider("other", LIVE_ONLY)
    registry["other"] = other
    source_session = _source_session(controller)
    session_key = await _start_source(controller, source_session)
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised"))
    provider.process_pcm_chunk = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    controller.feed_source_analysis(session_key, b"\x00" * ONE_SECOND)
    await _until(lambda: other.process_pcm_chunk.await_count == 1)
    await _until(lambda: source_session.stream_metadata is None)
    # a recognition still in flight when it was evicted reports too late
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised late"))

    assert source_session.stream_metadata is None
    assert controller._active_sessions[session_key] == {"other"}


@pytest.mark.asyncio
async def test_the_plugins_own_metadata_is_never_withdrawn(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """What the plugin reported stays when the analysis that reported before it ends."""
    provider = _recognizer(controller, registry)
    source_session = _source_session(controller)
    session_key = await _start_source(controller, source_session)
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised"))
    owners = StreamMetadata(title="From the plugin")
    _players(controller).update_source_metadata(
        PLAYER_ID, source_session.source_id, SOURCE_PROVIDER_ID, owners
    )

    await _stop_source(controller, session_key)

    assert source_session.stream_metadata is owners
    assert source_session.stream_metadata_reported is True


@pytest.mark.asyncio
async def test_a_report_from_a_superseded_stream_is_dropped(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """After a reconnect of the same source, the old stream's analysis shows nothing."""
    provider = _recognizer(controller, registry)
    source_session = _source_session(controller)
    old_key = await _start_source(controller, source_session)
    # a reconnect keeps the session object, with a new stream claiming it
    source_session.stream_session_id = "stream-2"
    new_key = await _start_source(controller, source_session)
    current = StreamMetadata(title="Recognised now")
    provider.report_stream_metadata(new_key, current)

    provider.report_stream_metadata(old_key, StreamMetadata(title="Recognised before"))
    await _stop_source(controller, old_key)

    assert source_session.stream_metadata is current


@pytest.mark.asyncio
async def test_metadata_reported_for_a_session_that_is_not_a_live_source_does_nothing(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A report about a radio stream is not shown anywhere."""
    provider = _recognizer(controller, registry)
    streamdetails = StreamDetails(
        provider=RADIO_ID,
        item_id="station",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.RADIO,
        stream_type=StreamType.HTTP,
        path="https://example.com/station.mp3",
        queue_id=PLAYER_ID,
    )
    await controller.start_analysis(AudioBuffer(PCM_FORMAT), streamdetails)
    (session_key,) = provider._sessions
    assert session_key.startswith(f"{streamdetails.uri}#")

    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised"))

    assert streamdetails.stream_metadata is None
    cast("MagicMock", controller.mass).player_queues.signal_update.assert_not_called()
    assert not _players(controller).updated_players


@pytest.mark.asyncio
async def test_a_hanging_or_failing_cancel_holds_up_no_other_provider(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider's cancel hangs, one raises: the next provider is released all the same."""
    monkeypatch.setattr(
        audio_analysis_module, "LIVE_SOURCE_CANCEL_TIMEOUT_SECONDS", 0.05, raising=False
    )
    hanging = _recognizer(controller, registry, "hanging")
    raising = _aa_provider("raising", LIVE_ONLY)
    raising.cancel = AsyncMock(side_effect=RuntimeError("boom"))
    registry["raising"] = raising
    released = _recognizer(controller, registry, "released")
    session_key = await _start_source(controller, _source_session(controller))
    hanging.cancel_gate = gate = asyncio.Event()
    worker = controller._workers[session_key]

    try:
        controller.stop_source_analysis(session_key)
        await _ended(worker)
    finally:
        gate.set()

    assert not released._sessions
    _assert_nothing_left(controller)
    warnings = cast("MagicMock", controller.logger).warning.call_args_list
    failed = [call.args[1] for call in warnings if "Failed to cancel" in call.args[0]]
    assert failed == [hanging.name, raising.name]


@pytest.mark.asyncio
async def test_a_second_cancel_during_the_release_leaves_no_registry_entry(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A worker cancelled again while it releases its providers still clears every registry."""
    provider = _recognizer(controller, registry)
    session_key = await _start_source(controller, _source_session(controller))
    provider.cancel_gate = gate = asyncio.Event()
    worker = controller._workers[session_key]

    try:
        controller.stop_source_analysis(session_key)
        await _until(provider.cancelling.is_set)
        worker.cancel()
        await _ended(worker)
    finally:
        gate.set()

    _assert_nothing_left(controller)


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.asyncio
async def test_a_provider_unloaded_during_its_start_takes_no_part(
    controller: AudioAnalysisController, registry: dict[str, Any], replaced: bool
) -> None:
    """A provider unloaded, or reloaded under the same id, while it starts gets no audio."""
    other = _aa_provider("other", LIVE_ONLY)
    registry["other"] = other
    starting = _recognizer(controller, registry, "starting")
    starting.start_gate = gate = asyncio.Event()
    session_key = _start_analysis(controller, _source_session(controller))
    assert session_key is not None
    await _until(starting.starting.is_set)

    await _unload(starting)
    del registry["starting"]
    replacement_chunks = AsyncMock()
    if replaced:
        replacement = _recognizer(controller, registry, "starting")
        replacement.process_pcm_chunk = replacement_chunks  # type: ignore[method-assign]
    gate.set()
    controller.feed_source_analysis(session_key, b"\x00" * ONE_SECOND)
    await _until(lambda: other.process_pcm_chunk.await_count == 1)

    assert controller._active_sessions[session_key] == {"other"}
    replacement_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unloading_provider_gets_no_more_audio(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A provider on its way out gets nothing more, even before it is released."""
    leaving = _aa_provider("leaving", LIVE_ONLY)
    staying = _aa_provider("staying", LIVE_ONLY)
    registry.update(leaving=leaving, staying=staying)
    session_key = await _start_source(controller, _source_session(controller))

    leaving.unloading = True
    controller.feed_source_analysis(session_key, b"\x00" * ONE_SECOND)
    await _until(lambda: staying.process_pcm_chunk.await_count == 1)

    leaving.process_pcm_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_leaves_a_live_session_to_its_worker(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Shutting down cancels a live session on its provider once, by its worker."""
    prov = _aa_provider("follower", LIVE_ONLY)
    registry["follower"] = prov
    await _start_source(controller, _source_session(controller))

    await controller.close()

    prov.cancel.assert_awaited_once()
    _assert_nothing_left(controller)


@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.asyncio
async def test_a_raw_stream_is_analysed_only_for_its_own_claim(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    superseded: bool,
) -> None:
    """A raw handle iterated after its source was selected again starts nothing."""
    prov = _aa_provider("follower", LIVE_ONLY)
    registry["follower"] = prov
    players = _players(controller)
    session = _source_session(controller)
    streams = MagicMock()
    streams.mass.players = players
    streams.audio = _audio(controller)
    streams._count_as_output_stream = lambda chunks: chunks
    streams._close_audio_source = AsyncMock()

    async def _source_pcm(*_args: Any) -> AsyncGenerator[bytes]:
        for _ in range(3):
            yield b"\x00" * ONE_SECOND

    monkeypatch.setattr(streams.audio, "_iter_audio_source_pcm", _source_pcm)
    handle = AudioSourceStream(
        streams,
        session,
        MagicMock(),
        _source_streamdetails(),
        session.playback_session_id,
        STREAM_TOKEN,
    )
    if superseded:
        # the source is selected again and a new stream request claims it
        players._start_audio_source_session(PLAYER_ID, session.source, SOURCE_PROVIDER_ID)
        players.claim_audio_source_session(session, session.playback_session_id, "stream-2")

    received = [chunk async for chunk in handle.stream(PCM_FORMAT)]

    assert len(received) == (0 if superseded else 3)
    assert prov.start_analysis.await_count == (0 if superseded else 1)
    await _until(lambda: not controller._workers)
    _assert_nothing_left(controller)


@pytest.mark.asyncio
async def test_a_refused_report_does_not_replace_what_is_withdrawn(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """What the source shows is withdrawn at the end, even after a later report was refused."""
    provider = _recognizer(controller, registry)
    source_session = _source_session(controller)
    session_key = await _start_source(controller, source_session)
    provider.report_stream_metadata(session_key, StreamMetadata(title="Shown"))
    # a reconnect claims the session for a new stream, which refuses this analysis' reports
    source_session.stream_session_id = "stream-2"
    provider.report_stream_metadata(session_key, StreamMetadata(title="Refused"))

    await _stop_source(controller, session_key)

    assert source_session.stream_metadata is None


@pytest.mark.parametrize("leaving", ["unloading", "released", "evicted"])
@pytest.mark.asyncio
async def test_a_report_from_a_provider_no_longer_taking_part_is_ignored(
    controller: AudioAnalysisController, registry: dict[str, Any], leaving: str
) -> None:
    """A late report from a provider on its way out never puts its title back."""
    provider = _recognizer(controller, registry)
    other = _aa_provider("other", LIVE_ONLY)
    registry["other"] = other
    source_session = _source_session(controller)
    session_key = await _start_source(controller, source_session)
    provider.cancel_gate = gate = asyncio.Event()

    try:
        if leaving == "unloading":
            provider.unloading = True
        elif leaving == "released":
            controller.release_provider(provider.instance_id)
        else:
            provider.process_pcm_chunk = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
            controller.feed_source_analysis(session_key, b"\x00" * ONE_SECOND)
            # evicted, and still releasing its session
            await _until(provider.cancelling.is_set)
        provider.report_stream_metadata(session_key, StreamMetadata(title="Too late"))
    finally:
        gate.set()

    assert source_session.stream_metadata is None
    assert not _players(controller).updated_players


# --- what analysis providers recognise on a radio station ---

# a listener that hears what the reader reads, so a report is due at once
_LISTENER_CAUGHT_UP = 1_000_000.0


def _station(queue_id: str = PLAYER_ID) -> StreamDetails:
    """Return the stream details of the station for a queue that plays it."""
    return StreamDetails(
        provider=RADIO_ID,
        item_id="station",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.RADIO,
        stream_type=StreamType.HTTP,
        path="https://example.com/station.mp3",
        queue_id=queue_id,
    )


async def _play_station(
    controller: AudioAnalysisController,
    streamdetails: StreamDetails,
    position: float | None = _LISTENER_CAUGHT_UP,
) -> AudioBuffer:
    """
    Start a buffer of the station for the queue of its stream details.

    :param position: The media second the queue's listener hears, None when not playing.
    """
    audio_buffer = AudioBuffer(PCM_FORMAT, mode=BufferMode.ROLLING)
    audio_buffer.playback_position = lambda _buffer: position
    streamdetails.buffer = audio_buffer
    await controller.start_analysis(audio_buffer, streamdetails)
    return audio_buffer


async def _hear(audio_buffer: AudioBuffer, seconds: int = 1) -> None:
    """Add seconds of audio to the buffer the analysis reads, and let the analysis read them."""
    for _ in range(seconds):
        await audio_buffer._put(b"\x00" * ONE_SECOND)
        # until the reader waits for audio again
        for _ in range(50):
            await asyncio.sleep(0)


def _radio_key(provider: AudioAnalysisProvider) -> str:
    (session_key,) = provider._sessions
    return session_key


@pytest.mark.asyncio
async def test_recognised_song_shows_on_every_queue_that_plays_the_station(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Every queue on the station shows the song, a queue the analysis does not follow does not."""
    provider = _recognizer(controller, registry)
    first, second, stopped = _station("queue_a"), _station("queue_b"), _station("queue_c")
    first_buffer = await _play_station(controller, first)
    await _play_station(controller, second)
    await (await _play_station(controller, stopped)).clear()
    # a buffer that started while no provider was loaded never joined the analysis
    unfollowed = _station("queue_d")
    unfollowed.buffer = AudioBuffer(PCM_FORMAT, mode=BufferMode.ROLLING)
    recognised = StreamMetadata(title="Recognised", artist="Someone")

    assert provider.report_stream_metadata(_radio_key(provider), recognised) is True
    await _hear(first_buffer)

    assert controller.shown_stream_metadata(first) is recognised
    assert controller.shown_stream_metadata(second) is recognised
    assert controller.shown_stream_metadata(stopped) is None
    assert controller.shown_stream_metadata(unfollowed) is None
    assert _players(controller).updated_players == ["queue_a", "queue_b"]


@pytest.mark.asyncio
async def test_recognised_song_never_shows_on_a_station_with_metadata(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A station that sends metadata itself refuses reports, on every queue that plays it."""
    provider = _recognizer(controller, registry)
    with_metadata, without = _station("queue_a"), _station("queue_b")
    stations_own = StreamMetadata(title="From the station")
    with_metadata.stream_metadata = stations_own
    audio_buffer = await _play_station(controller, with_metadata)
    await _play_station(controller, without)

    accepted = provider.report_stream_metadata(
        _radio_key(provider), StreamMetadata(title="Recognised")
    )
    await _hear(audio_buffer)

    assert accepted is False
    assert controller.shown_stream_metadata(with_metadata) is stations_own
    assert controller.shown_stream_metadata(without) is None
    assert not _players(controller).updated_players


@pytest.mark.asyncio
async def test_the_stations_own_metadata_wins_for_the_rest_of_the_session(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A recognised song yields once the station sends metadata, also after it clears it."""
    provider = _recognizer(controller, registry)
    streamdetails = _station()
    audio_buffer = await _play_station(controller, streamdetails)
    session_key = _radio_key(provider)
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised"))
    await _hear(audio_buffer)

    stations_own = StreamMetadata(title="From the station")
    streamdetails.stream_metadata = stations_own
    assert controller.shown_stream_metadata(streamdetails) is stations_own
    await _hear(audio_buffer)
    assert _players(controller).updated_players == [PLAYER_ID, PLAYER_ID]
    assert provider.report_stream_metadata(session_key, StreamMetadata(title="Later")) is False

    # talk on the station: it sends no metadata for a while
    streamdetails.stream_metadata = None
    await _hear(audio_buffer)

    assert controller.shown_stream_metadata(streamdetails) is None
    assert provider.report_stream_metadata(session_key, StreamMetadata(title="Talk")) is False


@pytest.mark.parametrize(("position", "lead"), [(3.0, 7), (None, 10)])
@pytest.mark.asyncio
async def test_recognised_song_shows_once_the_listener_reaches_it(
    controller: AudioAnalysisController,
    registry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    position: float | None,
    lead: int,
) -> None:
    """
    A report waits for the reader's lead over what the listener hears, its position moved along.

    Without a playing queue the audio the buffer holds is the lead.
    """
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        audio_analysis_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, time=time.time),
    )
    provider = _recognizer(controller, registry)
    streamdetails = _station()
    audio_buffer = await _play_station(controller, streamdetails, position)
    await _hear(audio_buffer, 10)
    recognised = StreamMetadata(
        title="Recognised", elapsed_time=20, elapsed_time_last_updated=1000.0
    )
    provider.report_stream_metadata(_radio_key(provider), recognised)

    clock.now += lead - 0.1
    await _hear(audio_buffer)
    assert controller.shown_stream_metadata(streamdetails) is None

    clock.now += 0.1
    await _hear(audio_buffer)
    shown = controller.shown_stream_metadata(streamdetails)
    assert shown is not None
    assert shown.title == "Recognised"
    assert shown.elapsed_time == 20
    assert shown.elapsed_time_last_updated == 1000.0 + lead
    assert recognised.elapsed_time_last_updated == 1000.0


@pytest.mark.parametrize("leaving", ["unloading", "released", "hang_guard"])
@pytest.mark.asyncio
async def test_recognised_song_is_withdrawn_when_its_provider_leaves(
    controller: AudioAnalysisController, registry: dict[str, Any], leaving: str
) -> None:
    """The song goes when the provider that recognised it leaves, the session goes on."""
    provider = _recognizer(controller, registry)
    other = _aa_provider("other", frozenset({MediaType.RADIO}))
    registry["other"] = other
    streamdetails = _station()
    audio_buffer = await _play_station(controller, streamdetails)
    session_key = _radio_key(provider)
    provider.report_stream_metadata(session_key, StreamMetadata(title="Recognised"))
    await _hear(audio_buffer)
    assert controller.shown_stream_metadata(streamdetails) is not None

    if leaving == "unloading":
        provider.unloading = True
    elif leaving == "released":
        controller.release_provider(provider.instance_id)
    else:
        # what the hang guard raises when a chunk takes too long
        provider.process_pcm_chunk = AsyncMock(side_effect=TimeoutError)  # type: ignore[method-assign]
    await _hear(audio_buffer)

    assert controller.shown_stream_metadata(streamdetails) is None
    assert _players(controller).updated_players == [PLAYER_ID, PLAYER_ID]
    assert "other" in controller._active_sessions[session_key]


@pytest.mark.asyncio
async def test_recognised_song_ends_with_its_session_and_survives_a_restart_of_the_stream(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """A restart within the stream keeps the song, a stop and play on the station shows none."""
    provider = _recognizer(controller, registry)
    streamdetails = _station()
    audio_buffer = await _play_station(controller, streamdetails)
    provider.report_stream_metadata(_radio_key(provider), StreamMetadata(title="Recognised"))
    await _hear(audio_buffer)

    # a restart of the source within the producer keeps the buffer
    streamdetails.path = "https://example.com/station-backup.mp3"
    await _hear(audio_buffer)
    assert controller.shown_stream_metadata(streamdetails) is not None

    # a stop and play reuses the stream details with a new buffer
    await audio_buffer.clear()
    await _until(lambda: not controller._workers)
    await _play_station(controller, streamdetails)

    assert controller.shown_stream_metadata(streamdetails) is None
    (radio,) = controller._radio_analyses.values()
    assert radio.shown is None


@pytest.mark.asyncio
async def test_a_report_about_a_track_or_unknown_session_is_refused(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """Only a live source or a radio station takes reports."""
    provider = _recognizer(controller, registry)
    track_provider = _aa_provider("tracks", frozenset({MediaType.TRACK}))
    registry["tracks"] = track_provider
    track = StreamDetails(
        provider=RADIO_ID,
        item_id="track",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="https://example.com/track.mp3",
        queue_id=PLAYER_ID,
    )
    track.buffer = AudioBuffer(PCM_FORMAT)
    await controller.start_analysis(track.buffer, track)
    recognised = StreamMetadata(title="Recognised")

    assert controller.show_stream_metadata(track_provider, track.uri, recognised) is False
    assert provider.report_stream_metadata("unknown", recognised) is False
    assert controller.show_stream_metadata(provider, f"{track.uri}#0000", recognised) is False
    assert controller.shown_stream_metadata(track) is None
    assert not _players(controller).updated_players


@pytest.mark.asyncio
async def test_recognised_song_is_never_written_to_the_stream_details(
    controller: AudioAnalysisController, registry: dict[str, Any]
) -> None:
    """The stream details keep only what the station sent while a song shows and goes."""
    provider = _recognizer(controller, registry)
    streamdetails = _station()
    audio_buffer = await _play_station(controller, streamdetails)
    before = dict(vars(streamdetails))
    provider.report_stream_metadata(_radio_key(provider), StreamMetadata(title="Recognised"))
    await _hear(audio_buffer)
    assert controller.shown_stream_metadata(streamdetails) is not None

    await audio_buffer.clear()
    await _until(lambda: not controller._workers)

    assert vars(streamdetails) == before
    assert all(vars(streamdetails)[name] is value for name, value in before.items())
    assert streamdetails.stream_metadata is None
    assert streamdetails.stream_title is None
