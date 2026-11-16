"""Tests for analysis of live sources and the live metadata analysis providers report."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat, AudioSource
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.players.audio_sources import AudioSourceMixin, AudioSourceSession
from music_assistant.controllers.players.state_updates import PLAYER_UPDATE_DEBOUNCE
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
LIVE_ONLY = frozenset({MediaType.AUDIO_SOURCE})


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


class _Players(AudioSourceMixin):
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
    session.stream_session_id = "stream-1"
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


async def _until(condition: Any) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0)


async def _start_source(
    controller: AudioAnalysisController, source_session: AudioSourceSession
) -> str:
    """Start a live source session and wait until its providers took it."""
    session_key = controller.start_source_analysis(
        source_session, _source_streamdetails(), PCM_FORMAT
    )
    assert session_key is not None
    await _until(lambda: session_key in controller._active_sessions)
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

    session_key = controller.start_source_analysis(
        _source_session(controller, allow_audio_analysis=False),
        _source_streamdetails(),
        PCM_FORMAT,
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
        _source_session(controller), _source_streamdetails(), PCM_FORMAT
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
        _source_session(controller), _source_streamdetails(), PCM_FORMAT
    )

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
            _source_streamdetails(), PCM_FORMAT, source_session=_source_session(controller)
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
            _source_streamdetails(), PCM_FORMAT, source_session=source_session
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
                streamdetails, PCM_FORMAT, source_session=_source_session(controller)
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

    session_key = controller.start_source_analysis(
        _source_session(controller), _source_streamdetails(), PCM_FORMAT
    )
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

    session_key = controller.start_source_analysis(
        _source_session(controller), _source_streamdetails(), PCM_FORMAT
    )
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
    session_key = controller.start_source_analysis(
        _source_session(controller), _source_streamdetails(), PCM_FORMAT
    )
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

    await provider.unload()
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

    session_key = controller.start_source_analysis(
        _source_session(controller), _source_streamdetails(), PCM_FORMAT
    )

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
        await provider.unload()

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
    assert streamdetails.uri in provider._sessions

    provider.report_stream_metadata(streamdetails.uri, StreamMetadata(title="Recognised"))

    assert streamdetails.stream_metadata is None
    cast("MagicMock", controller.mass).player_queues.signal_update.assert_not_called()
    assert not _players(controller).updated_players
