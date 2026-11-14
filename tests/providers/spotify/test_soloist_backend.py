"""
Unit tests for the Spotify Soloist playback backend.

The backend plays a queue through one engine run per playback session: the item
that follows the one being streamed is queued on the engine ahead of time, and
the capture is cut where the engine moves on to it. These tests lock down the
logic around that: the capture's pacing, lead-silence trimming, each item's
cushion and the sink backpressure, tail-padding suppression, feeding the next
item and cutting at the boundary, moving the running engine (seek, skip, jump),
idling out, startup and event handling, delivery validation, item acquisition
(busy/claimed/superseded), paired-session adoption and setup. No PulseAudio is
involved, and a real child process stands in for the engine only where the
run's teardown is under test.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, PlaybackState
from music_assistant_models.errors import AudioError, LoginFailed
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers import process as process_module
from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.music_provider import ProviderStreamLimitError
from music_assistant.providers.spotify.backends import StreamSupersededError
from music_assistant.providers.spotify.backends import soloist as soloist_backend
from music_assistant.providers.spotify.backends.soloist import (
    _BYTES_PER_SECOND,
    _FEED_LEAD_S,
    _FRAME_BYTES,
    _MAX_LEAD_TRIM_S,
    _MAX_SWITCH_TRIM_S,
    _PACE_BURST_S,
    _PACE_RATE,
    _READ_CHUNK_SIZE,
    _TAIL_PAD_GRACE_S,
    _TAIL_PAD_ZONE_S,
    SoloistBackend,
    _EngineRun,
    _Item,
    _trim_lead_silence,
)
from music_assistant.providers.spotify.constants import (
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
)
from music_assistant.providers.spotify.helpers import soloist_session_present
from music_assistant.providers.spotify.provider import SpotifyProvider
from music_assistant.providers.spotify_connect.soloist.runtime import (
    WS_ADDR_FILE,
    WS_PORT_FILE,
    SoloistAuthState,
    SoloistEntity,
    SoloistError,
    SoloistEvent,
    SoloistOptionsChanged,
    SoloistPlaybackOptions,
    SoloistPlaybackState,
    SoloistPosition,
    SoloistQueueChanged,
    SoloistQueueEntry,
    SoloistTrackChanged,
    SoloistVolumeChanged,
)

TRACK_A = "spotify:track:aaa"
TRACK_B = "spotify:track:bbb"
TRACK_C = "spotify:track:ccc"
# an audiobook is one item whose chapters are separate Spotify URIs
AUDIOBOOK = "spotify:show:book"
CHAPTER_A = "spotify:episode:ch1"
CHAPTER_B = "spotify:episode:ch2"
CHAPTER_C = "spotify:episode:ch3"

# Stands in for an engine that honours SIGINT but takes a moment to exit on it,
# longer than the (shortened) wait close() grants the run's log reader.
_SLOW_EXIT_ENGINE = (
    "import signal, sys, time; "
    "signal.signal(signal.SIGINT, lambda *_: (time.sleep(1), sys.exit(0))); "
    "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(30)"
)


def test_trim_drops_an_all_zero_chunk_within_the_bound() -> None:
    """A pure-silence chunk inside the trim budget is dropped entirely."""
    chunk = b"\x00" * 1024
    trimmed, skipped = _trim_lead_silence(chunk, 0)
    assert trimmed == b""
    assert skipped == 1024


def test_trim_keeps_frame_alignment_when_audio_starts_mid_chunk() -> None:
    """Audio starting mid-chunk is cut on a sample-frame boundary."""
    # audio starts one byte into the third frame: the trim must keep that frame whole
    chunk = b"\x00" * (_FRAME_BYTES * 2 + 1) + b"\x01" * 64
    trimmed, skipped = _trim_lead_silence(chunk, 0)
    assert skipped == _FRAME_BYTES * 2
    assert len(trimmed) % _FRAME_BYTES == 1  # the partial frame's remainder is preserved
    assert trimmed.endswith(b"\x01" * 64)


def test_trim_passes_silence_through_once_the_bound_is_exceeded() -> None:
    """Beyond the trim budget, silence is genuine content and is delivered."""
    chunk = b"\x00" * 1024
    trimmed, skipped = _trim_lead_silence(chunk, int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND))
    assert trimmed == chunk
    assert skipped == 0


def test_the_lead_trim_never_exceeds_its_budget() -> None:
    """Silence beyond the budget is content, including where audio starts mid-chunk."""
    budget = int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND)
    # already at the budget, with a chunk whose silence runs well past it
    chunk = b"\x00" * 4096 + b"\x01" * 64
    trimmed, skipped = _trim_lead_silence(chunk, budget - _FRAME_BYTES)
    assert skipped == _FRAME_BYTES
    assert len(trimmed) == len(chunk) - _FRAME_BYTES


async def test_a_superseded_audiobook_stream_stops_instead_of_stitching_on(
    tmp_path: Path,
) -> None:
    """The chapters after a seek belong to the stream that took over, not to this one."""
    provider = _make_provider(tmp_path)
    calls: list[str] = []

    async def _cut(uri: str, *_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        calls.append(uri)
        yield b"audio"
        raise StreamSupersededError("replaced")

    provider.backend = MagicMock(stream_spotify_uri=_cut)
    streamdetails = MagicMock(
        media_type=MediaType.AUDIOBOOK,
        data={"chapters": [CHAPTER_A, CHAPTER_B], "chapters_data": []},
    )
    chunks = [chunk async for chunk in provider.get_audio_stream(streamdetails)]
    assert chunks == [b"audio"]
    assert calls == [CHAPTER_A]


async def test_only_the_chapter_a_stream_starts_on_may_take_the_session(
    tmp_path: Path,
) -> None:
    """The chapter a seek lands on starts the stream; the ones after it continue it."""
    provider = _make_provider(tmp_path)
    calls: list[tuple[str, bool]] = []

    async def _stream(
        uri: str, _seek: int = 0, *, continuation: bool = False, **_kwargs: Any
    ) -> AsyncGenerator[bytes]:
        calls.append((uri, continuation))
        yield b"audio"

    provider.backend = MagicMock(stream_spotify_uri=_stream)
    streamdetails = MagicMock(
        media_type=MediaType.AUDIOBOOK,
        data={
            "chapters": [CHAPTER_A, CHAPTER_B, CHAPTER_C],
            "chapters_data": [{"duration_ms": 60_000}] * 3,
        },
    )
    async for _ in provider.get_audio_stream(streamdetails, seek_position=70):
        pass
    assert calls == [(CHAPTER_B, False), (CHAPTER_C, True)]


async def test_a_superseded_track_stream_ends_without_an_error(tmp_path: Path) -> None:
    """A replaced stream is no failure: the item plays on the stream that took over."""
    provider = _make_provider(tmp_path)

    async def _cut(_uri: str, *_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        yield b"audio"
        raise StreamSupersededError("replaced")

    provider.backend = MagicMock(stream_spotify_uri=_cut)
    streamdetails = MagicMock(media_type=MediaType.TRACK, item_id="aaa", data=None)
    chunks = [chunk async for chunk in provider.get_audio_stream(streamdetails)]
    assert chunks == [b"audio"]


async def test_an_audiobook_gives_up_on_capacity_instead_of_burning_chapters(
    tmp_path: Path,
) -> None:
    """Skipping ahead would cost the audiobook its availability and the caller its retry."""
    provider = _make_provider(tmp_path)
    calls: list[str] = []

    async def _refuse(uri: str, *_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        calls.append(uri)
        for _ in ():  # never yields; only makes this an async generator
            yield b""
        raise soloist_backend.SoloistSessionBusyError(provider)

    provider.backend = MagicMock(stream_spotify_uri=_refuse)
    streamdetails = MagicMock(
        media_type=MediaType.AUDIOBOOK,
        data={"chapters": [TRACK_A, TRACK_B, "spotify:track:ccc"], "chapters_data": []},
    )

    with pytest.raises(ProviderStreamLimitError):
        async for _ in provider.get_audio_stream(streamdetails):
            pass
    # the first chapter's refusal ends it: no chapter is skipped over
    assert calls == [TRACK_A]


def test_the_shaper_only_emits_whole_frames() -> None:
    """A read that ends mid-frame must never split a frame across two items."""
    shaper = soloist_backend._CaptureShaper()
    # the session's first bytes are infrastructure silence, and are dropped
    assert shaper.shape(b"\x00" * 4096) == b""
    # a mis-aligned read emits whole frames and carries the remainder
    first = shaper.shape(b"\x01" * (_FRAME_BYTES + 3))
    assert len(first) == _FRAME_BYTES
    # which is then completed by the next read, losing nothing
    second = shaper.shape(b"\x02" * (_FRAME_BYTES - 3))
    assert len(second) == _FRAME_BYTES
    assert second[:3] == b"\x01" * 3
    # an aligned read passes straight through
    assert shaper.shape(b"\x03" * _FRAME_BYTES) == b"\x03" * _FRAME_BYTES


def test_the_shaper_trims_lead_silence_only_once() -> None:
    """Silence after the audio has started is content, not pre-roll."""
    shaper = soloist_backend._CaptureShaper()
    assert shaper.shape(b"\x01" * _FRAME_BYTES) == b"\x01" * _FRAME_BYTES
    silence = b"\x00" * _FRAME_BYTES
    assert shaper.shape(silence) == silence


def test_the_engine_is_told_not_to_normalize(tmp_path: Path) -> None:
    """MA normalizes this audio itself, so the engine's own normalization is switched off."""
    backend = _make_backend(tmp_path)
    prefs = backend._data_dir / "settings" / "Users" / "alice-user" / "prefs"
    prefs.parent.mkdir(parents=True)
    prefs.write_text("some.engine.key=1\n", encoding="utf-8")
    backend._prepare_data_dir(normalize=False)
    content = prefs.read_text(encoding="utf-8").splitlines()
    assert "some.engine.key=1" in content
    assert "audio.normalize_v2=false" in content
    # MA mixes the queue's crossfade itself, so the engine's own is always off
    assert "audio.crossfade_v2=false" in content
    # the ceiling is stated rather than left to the engine's own default
    assert "audio.play_bitrate_enumeration=5" in content
    assert "audio.play_bitrate_non_metered_enumeration=5" in content
    assert "audio.play_bitrate_non_metered_migrated=true" in content


def test_disabling_crossfade_writes_the_boolean(tmp_path: Path) -> None:
    """Crossfade off is written explicitly, so a stale 'on' cannot survive."""
    backend = _make_backend(tmp_path)
    prefs = backend._data_dir / "settings" / "prefs"
    prefs.parent.mkdir(parents=True)
    prefs.write_text("audio.crossfade_v2=true\naudio.crossfade.time_v2=8000\n", encoding="utf-8")
    backend._prepare_data_dir(normalize=False)
    content = prefs.read_text(encoding="utf-8").splitlines()
    assert "audio.crossfade_v2=false" in content
    assert not any(line.startswith("audio.crossfade.time_v2") for line in content)


async def test_setup_requires_an_api_key(tmp_path: Path) -> None:
    """Without a stored API key the user must be sent back through the setup flow."""
    backend = _make_backend(tmp_path)
    with pytest.raises(LoginFailed) as err:
        await backend.setup()
    assert err.value.translation_key == "soloist_pairing_required"


async def test_setup_requires_a_paired_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An API key without a paired session also routes back to the setup flow."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20, CONF_SOLOIST_CONSENT: True})
    _install_fake_binary_manager(monkeypatch)
    with pytest.raises(LoginFailed) as err:
        await backend.setup()
    assert err.value.translation_key == "soloist_pairing_required"


async def test_streaming_without_setup_is_refused(tmp_path: Path) -> None:
    """A backend whose setup never ran refuses to stream instead of half-starting."""
    backend = _make_backend(tmp_path)
    with pytest.raises(AudioError, match="not started"):
        async for _ in backend.stream_spotify_uri(TRACK_A):
            pass


def test_session_present_detection(tmp_path: Path) -> None:
    """Only the engine's per-account state counts as paired."""
    data_dir = tmp_path / "soloist-data"
    assert soloist_session_present(data_dir) is False
    data_dir.mkdir()
    (data_dir / WS_ADDR_FILE).write_text("127.0.0.1", encoding="utf-8")
    (data_dir / WS_PORT_FILE).write_text("1234", encoding="utf-8")
    assert soloist_session_present(data_dir) is False
    # everything a spawn leaves behind outlives the pairing it ran on: the engine
    # keeps its identity, lock, cache and crash handler in the data dir even
    # though it is given a cache dir of its own, and Music Assistant writes the
    # prefs there before every spawn
    (data_dir / "settings").mkdir()
    (data_dir / "settings" / "prefs").write_text("audio.normalize_v2=false\n", encoding="utf-8")
    (data_dir / ".device_id").write_text("6b6c2a07", encoding="utf-8")
    (data_dir / ".lock").write_bytes(b"")
    (data_dir / "cache" / "Users" / "spotify-user-user").mkdir(parents=True)
    (data_dir / "crashpad").mkdir()
    assert soloist_session_present(data_dir) is False
    (data_dir / "settings" / "Users" / "spotify-user-user").mkdir(parents=True)
    assert soloist_session_present(data_dir) is True


# what one FIFO read typically holds: whole frames, 50 ms of capture
_CHUNK = _BYTES_PER_SECOND // 20


def _make_provider(tmp_path: Path, setup_data: dict[str, Any] | None = None) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with the given setup_data."""
    prov = object.__new__(SpotifyProvider)
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(return_value=None)
    config.values = {}
    prov.config = config
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.available = True
    mass = MagicMock()
    mass.storage_path = str(tmp_path / "storage")
    mass.cache_path = str(tmp_path / "cache")
    # get_setup_value reads the live setup_data blob from the store
    mass.config.get = MagicMock(return_value=setup_data or {})
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    # the store keeps values encrypted; decrypt is an identity map for the test
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    return prov


def _make_backend(tmp_path: Path, setup_data: dict[str, Any] | None = None) -> SoloistBackend:
    """Return a SoloistBackend on a mocked provider."""
    return SoloistBackend(_make_provider(tmp_path, setup_data))


def _streamable_backend(tmp_path: Path) -> SoloistBackend:
    """Return a backend that passes the started check, with no binary download."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20})
    backend._server = MagicMock()
    backend._binary = tmp_path / "soloist-bin"
    return backend


def _streamdetails_for(
    *,
    queue_id: str | None = "player1",
    uri: str = TRACK_A,
    media_type: MediaType = MediaType.TRACK,
) -> StreamDetails:
    """Return stream details for a Spotify item served by the test instance."""
    return StreamDetails(
        provider="spotify--test",
        item_id=uri.rsplit(":", 1)[1],
        audio_format=AudioFormat(content_type=ContentType.PCM_S16LE),
        media_type=media_type,
        queue_id=queue_id,
    )


def _install_fake_binary_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the shared binary manager so no download or exec is attempted."""
    manager = MagicMock()
    manager.ensure_fresh = AsyncMock(return_value=Path("/nonexistent/soloist"))
    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", MagicMock(return_value=manager))


def _make_run(
    tmp_path: Path,
    uri: str = TRACK_A,
    seek_ms: int = 0,
    duration: int | None = 180,
    *,
    streamdetails: StreamDetails | None = None,
    backend: SoloistBackend | None = None,
) -> _EngineRun:
    """Return a run claimed for the given item, with its process/sink/client replaced by mocks."""
    if streamdetails is None:
        streamdetails = _streamdetails_for(uri=uri)
    if duration is not None:
        streamdetails.duration = duration
    run = _EngineRun(backend or _make_backend(tmp_path), uri, seek_ms, streamdetails)
    run._sink = AsyncMock()
    client = AsyncMock()
    client.connected = True
    run._client = client
    run._proc = MagicMock(returncode=None)
    run._logged_in = True
    run._sink_running = True
    # a capture idling on the FIFO, as the running reader does between reads
    run._read_waiting = True
    return run


def _live_run(
    tmp_path: Path, duration: int | None = 180, *, backend: SoloistBackend | None = None
) -> _EngineRun:
    """Return a run whose first item is playing and whose audio is flowing."""
    run = _make_run(tmp_path, duration=duration, backend=backend)
    _swallow_tasks(run)
    run._play_sent = True
    run.current_item.started.set()
    run._reader = MagicMock()
    return run


def _swallow_tasks(run: _EngineRun) -> MagicMock:
    """Replace the run's task scheduling with a mock that discards the coroutines."""
    create_task = MagicMock(side_effect=lambda coro: coro.close())
    run.mass.create_task = create_task  # type: ignore[method-assign]
    return create_task


def _run_tasks(run: _EngineRun) -> list[asyncio.Task[Any]]:
    """Have the run's scheduled tasks actually run, and return them for joining."""
    tasks: list[asyncio.Task[Any]] = []

    def _create_task(coro: Any) -> asyncio.Task[Any]:
        tasks.append(task := asyncio.get_running_loop().create_task(coro))
        return task

    run.mass.create_task = MagicMock(side_effect=_create_task)  # type: ignore[method-assign]
    return tasks


def _client(run: _EngineRun) -> AsyncMock:
    """Return the run's mocked engine client."""
    return cast("AsyncMock", run._client)


def _sink(run: _EngineRun) -> AsyncMock:
    """Return the run's mocked capture sink."""
    return cast("AsyncMock", run._sink)


async def _capture(run: _EngineRun, data: bytes) -> None:
    """Route capture bytes through the run the way the FIFO reader hands them over."""
    for offset in range(0, len(data), _CHUNK):
        assert await run._route(data[offset : offset + _CHUNK])


def _fed(run: _EngineRun) -> object:
    """Return the item queued on the engine, read afresh."""
    return run._fed


def _upcoming(run: _EngineRun) -> _Item | None:
    """Return the item the capture moves on to, read afresh."""
    return run._next


def _idle_watch(run: _EngineRun) -> asyncio.Task[None] | None:
    """Return the run's idle watch, read afresh."""
    return run._idle_watch


def _unclaimed_since(item: _Item) -> float | None:
    """Return when the item's unclaimed clock started, read afresh."""
    return item.unclaimed_since


def _cushioned(item: _Item) -> bytes:
    """Return the audio waiting in an item's cushion, without the end sentinel."""
    collected = bytearray()
    while not item.chunks.empty():
        if (chunk := item.chunks.get_nowait()) is not None:
            collected.extend(chunk)
    return bytes(collected)


async def _collect(
    run: _EngineRun, item: _Item | None = None, *, into: bytearray | None = None
) -> bytes:
    """Return everything the run streams for an item, up to where its delivery ended."""
    collected = bytearray() if into is None else into
    async for chunk in run.stream(item or run.current_item):
        collected.extend(chunk)
    return bytes(collected)


def _audio(seconds: float, value: int = 1) -> bytes:
    """Return the given length of non-silent capture."""
    return bytes([value]) * (round(seconds * _BYTES_PER_SECOND) // _FRAME_BYTES * _FRAME_BYTES)


def _silence(seconds: float) -> bytes:
    """Return the given length of digital silence."""
    return _audio(seconds, 0)


def _daemon_saying(*lines: str) -> MagicMock:
    """Return a process whose captured output is the given lines."""

    async def _iter_stdout() -> AsyncGenerator[str]:
        for line in lines:
            yield line

    return MagicMock(iter_stdout=_iter_stdout)


def _playback_event(
    status: str,
    *,
    uri: str | None = None,
    position_ms: int | None = None,
    duration_ms: int | None = None,
) -> SoloistEvent:
    """Return a playback_changed event, optionally with the item and its exact length."""
    item = None
    if uri is not None:
        decorations = {"playback": {"duration_ms": duration_ms}} if duration_ms else {}
        item = SoloistEntity(uri=uri, entity_type="track", decorations=decorations)
    position = (
        SoloistPosition(position_ms=position_ms, timestamp_ms=0)
        if position_ms is not None
        else None
    )
    return SoloistEvent(
        type="playback_changed",
        data=SoloistPlaybackState(status=status, item=item, position=position),
        raw={},
    )


def _track_event(uri: str) -> SoloistEvent:
    """Return a track_changed event to the given item."""
    return SoloistEvent(
        type="track_changed",
        data=SoloistTrackChanged(item=SoloistEntity(uri=uri, entity_type="track")),
        raw={},
    )


def _queue_event(*entries: tuple[str, str]) -> SoloistEvent:
    """Return a queue_changed event listing the given (uid, uri) entries as queued."""
    upcoming = [
        SoloistQueueEntry(uid=uid, source="queue", item=SoloistEntity(uri=uri, entity_type="track"))
        for uid, uri in entries
    ]
    # what the context plays next is listed too, and is never a queued entry
    upcoming.append(
        SoloistQueueEntry(
            uid="ctx", source="context", item=SoloistEntity(uri=TRACK_C, entity_type="track")
        )
    )
    return SoloistEvent(type="queue_changed", data=SoloistQueueChanged(upcoming=upcoming), raw={})


async def _feed(run: _EngineRun, uri: str = TRACK_B, uid: str = "q1") -> None:
    """Feed an item to follow the current one, and have the engine list it in its queue."""
    await run.feed_next(cast("str", run.current_item.media_key), uri)
    await run._handle_event(_queue_event((uid, uri)))


async def _reach_fed_item(run: _EngineRun) -> tuple[_Item, _Item]:
    """Have the engine move on to a fed TRACK_B, returning the outgoing and the next item."""
    outgoing = run.current_item
    await _feed(run)
    await _capture(run, _audio(1.5))
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))
    assert run._next is not None
    return outgoing, run._next


async def _cross_fed_boundary(run: _EngineRun) -> tuple[_Item, _Item]:
    """Play the current item into a fed TRACK_B, returning the outgoing and the next item."""
    outgoing, upcoming = await _reach_fed_item(run)
    # the capture runs on past the cut
    await _capture(run, _audio(0.5, 2))
    assert run.current_item is upcoming
    return outgoing, upcoming


def test_scrub_leaves_mid_track_silence_alone(tmp_path: Path) -> None:
    """A quiet passage outside the tail zone is content (the shaper owns the lead)."""
    run = _make_run(tmp_path)
    item = run.current_item
    item.read_bytes = 10 * _BYTES_PER_SECOND
    assert run._scrub(item, b"\x00" * 1024) == b"\x00" * 1024
    assert run._scrub(item, b"\x01" * 64) == b"\x01" * 64


def test_scrub_refuses_padding_in_the_items_tail_zone(tmp_path: Path) -> None:
    """Zeros inside the tail zone are the sink idling out the engine's end."""
    run = _make_run(tmp_path, duration=60)
    item = run.current_item
    second = _BYTES_PER_SECOND
    item.read_bytes = 55 * second
    grace = int(_TAIL_PAD_GRACE_S * second)
    # the first moment of padding is kept, the rest refused
    assert run._scrub(item, b"\x00" * grace) == b"\x00" * grace
    item.tail_zeros = grace
    assert run._scrub(item, b"\x00" * second) == b""
    # real audio resets the count: the zeros were a quiet passage after all
    assert run._scrub(item, b"\x01" * 64) == b"\x01" * 64
    assert item.tail_zeros == 0


def test_scrub_leaves_a_short_items_silence_alone(tmp_path: Path) -> None:
    """An item no longer than the zone has no distinguishable tail."""
    run = _make_run(tmp_path, duration=int(_TAIL_PAD_ZONE_S))
    item = run.current_item
    item.read_bytes = int(_TAIL_PAD_ZONE_S - 1) * _BYTES_PER_SECOND
    chunk = b"\x00" * (2 * int(_TAIL_PAD_GRACE_S * _BYTES_PER_SECOND))
    assert run._scrub(item, chunk) == chunk


async def test_a_full_cushion_pauses_the_engine(tmp_path: Path) -> None:
    """When the consumer stops taking audio, the sink is suspended, not overflowed."""
    run = _make_run(tmp_path)
    item = run.current_item
    sink = _sink(run)
    while not item.chunks.full():
        item.chunks.put_nowait(b"\x01")

    blocked = asyncio.ensure_future(run._hand_over(item, b"\x02"))
    await asyncio.sleep(0.01)
    assert not blocked.done()
    sink.suspend.assert_awaited_once()

    # the consumer takes a chunk: the write lands and the engine resumes
    run._engine_playing = True
    assert item.chunks.get_nowait() == b"\x01"
    assert await blocked is True
    sink.resume.assert_awaited()


async def test_a_full_cushion_still_ends_the_stream(tmp_path: Path) -> None:
    """The end of delivery survives a cushion with no room left for the sentinel."""
    # no duration: this covers the cushion, not how much audio arrived
    run = _make_run(tmp_path, duration=None)
    item = run.current_item
    while not item.chunks.full():
        item.chunks.put_nowait(b"\x01" * 64)
    run._finish_delivery(item)
    # nothing may re-signal the end once the consumer drains: the flag carries it
    assert len(await _collect(run)) == item.chunks.maxsize * 64


async def test_a_failed_run_surfaces_its_error_to_the_stream(tmp_path: Path) -> None:
    """The consumer sees the run's real failure, not a clean end."""
    run = _make_run(tmp_path)
    _swallow_tasks(run)
    run.current_item.chunks.put_nowait(b"\x01" * 64)
    run._fail("the engine broke")
    with pytest.raises(AudioError, match="the engine broke"):
        await _collect(run)


async def test_a_run_that_delivered_nothing_is_rejected(tmp_path: Path) -> None:
    """A run still going that rendered nothing must not read as a completed stream."""
    run = _make_run(tmp_path, duration=152)
    run.current_item.chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._finish_delivery(run.current_item)
    with pytest.raises(AudioError, match="stopped before it played"):
        await _collect(run)


def _end_with(run: _EngineRun, audio: bytes) -> _Item:
    """Have the engine end the current item after the given audio, and return the item."""
    item = run.current_item
    item.chunks.put_nowait(audio)
    item.raw_read = item.end_at = len(audio)
    run._finish_delivery(item)
    return item


async def test_a_refused_item_is_reported_as_a_refusal(tmp_path: Path) -> None:
    """An engine that plays nothing of an item and moves on was refused the item."""
    run = _make_run(tmp_path, duration=152)
    _end_with(run, b"\x01" * _FRAME_BYTES)
    # the message travels on the error itself; the queue reports it where it skips
    with pytest.raises(AudioError, match="would not play this track"):
        await _collect(run)


@pytest.mark.parametrize("returncode", [0, 1])
async def test_a_crashed_run_is_not_reported_as_a_refusal(tmp_path: Path, returncode: int) -> None:
    """An engine that died on its item is a fault, whatever it managed to play or exited with."""
    run = _make_run(tmp_path, duration=152)
    run._engine_exited = True
    run._proc = MagicMock(returncode=returncode)
    run.current_item.chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._finish_delivery(run.current_item)
    # names the code, so a refusal that turns out to exit is recognisable; the
    # engine never quits on its own, so even a clean exit is no natural end
    with pytest.raises(AudioError, match=f"engine exit code {returncode}"):
        await _collect(run)


async def test_audio_that_arrived_and_stopped_is_not_a_failure(tmp_path: Path) -> None:
    """Something ended the item early - a skip, the engine moving on, the account playing elsewhere."""
    run = _make_run(tmp_path, duration=152)
    _end_with(run, b"\x01" * (100 * _BYTES_PER_SECOND))
    await _collect(run)


async def test_a_crash_part_way_through_is_reported(tmp_path: Path) -> None:
    """Audio arrived, but the engine died on the item rather than ending it."""
    run = _make_run(tmp_path, duration=152)
    run._engine_exited = True
    run._proc = MagicMock(returncode=1)
    run.current_item.chunks.put_nowait(b"\x01" * (30 * _BYTES_PER_SECOND))
    run._finish_delivery(run.current_item)
    with pytest.raises(AudioError, match="stopped unexpectedly"):
        await _collect(run)


async def test_an_engine_exit_mid_item_fails_its_stream(tmp_path: Path) -> None:
    """The reader delivers what the engine rendered, then the stream fails with the exit code."""
    run = _make_run(tmp_path, duration=152)
    reader = asyncio.StreamReader()
    reader.feed_data(_audio(0.5))
    run._reader = reader
    run._engine_exited = True
    run._proc = MagicMock(returncode=1)
    async with asyncio.timeout(5):
        await run._read_capture()
    assert run.current_item.delivery_done
    collected = bytearray()
    with pytest.raises(AudioError, match="engine exit code 1"):
        await _collect(run, into=collected)
    assert bytes(collected) == _audio(0.5)


async def test_a_brief_item_that_played_nothing_is_still_reported(tmp_path: Path) -> None:
    """Accepting short items wholesale would let a refused one through unnoticed."""
    run = _make_run(tmp_path, duration=2)
    run.current_item.engine_duration_ms = 1200
    _end_with(run, b"\x01" * _FRAME_BYTES)
    with pytest.raises(AudioError, match="would not play this track"):
        await _collect(run)


async def test_a_brief_item_played_in_full_is_accepted(tmp_path: Path) -> None:
    """The lead trim takes its cut off a complete delivery of a very short item."""
    run = _make_run(tmp_path, duration=2)
    item = run.current_item
    item.engine_duration_ms = 1200
    # what a complete 1.2s item arrives as once the trim has taken its full budget
    item.shaper._lead_skipped = int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND)
    _end_with(run, b"\x01" * (7 * _BYTES_PER_SECOND // 10))
    await _collect(run)


async def test_a_short_item_that_played_nothing_is_still_reported(tmp_path: Path) -> None:
    """A short item is judged the same way: nothing arrived, so nothing played."""
    run = _make_run(tmp_path, duration=8)
    _end_with(run, b"\x01" * _FRAME_BYTES)
    with pytest.raises(AudioError, match="would not play this track"):
        await _collect(run)


async def test_a_short_item_played_in_full_is_accepted(tmp_path: Path) -> None:
    """A complete short item must not be mistaken for one that never played."""
    run = _make_run(tmp_path, duration=8)
    _end_with(run, b"\x01" * (8 * _BYTES_PER_SECOND))
    await _collect(run)


async def test_a_seek_to_the_items_end_is_not_read_as_a_refusal(tmp_path: Path) -> None:
    """Audio the seek skipped counts, so a near-complete delivery ends cleanly."""
    run = _make_run(tmp_path, duration=152, seek_ms=151_000)
    _end_with(run, b"\x01" * _FRAME_BYTES)
    await _collect(run)


async def test_a_stopped_run_is_not_judged_incomplete(tmp_path: Path) -> None:
    """A consumer that left early is the normal end of an aborted stream."""
    run = _make_run(tmp_path, duration=152)
    run.current_item.chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._stopped = True
    run._finish_delivery(run.current_item)
    assert await _collect(run) == b"\x01" * _FRAME_BYTES


async def test_stop_does_not_report_an_engine_that_exits_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An engine that takes its time to exit on SIGINT is not reported as unstoppable.

    The run's log reader stays on the engine's stdout across the close, so the
    close has to reap the engine without reading that pipe itself; the warning
    is reserved for an engine that survived the SIGKILL escalation.
    """
    monkeypatch.setattr(process_module, "STREAM_RELEASE_TIMEOUT", 0.2)
    run = _make_run(tmp_path)
    ready = asyncio.Event()
    run.logger = MagicMock()
    run.logger.debug.side_effect = lambda _fmt, line: ready.set() if line == "ready" else None
    proc = AsyncProcess(
        [sys.executable, "-c", _SLOW_EXIT_ENGINE], stdout=True, stderr=asyncio.subprocess.STDOUT
    )
    await proc.start()
    run._proc = proc
    run._log_task = asyncio.create_task(run._log_output(proc))
    await asyncio.wait_for(ready.wait(), 5)

    async with asyncio.timeout(5):
        await run.stop()

    run.logger.warning.assert_not_called()
    assert proc.returncode == 0


async def test_a_seek_counts_towards_the_delivery(tmp_path: Path) -> None:
    """Audio skipped by the seek is not audio the engine failed to deliver."""
    run = _make_run(tmp_path, duration=60, seek_ms=55_000)
    run.current_item.chunks.put_nowait(b"\x01" * (6 * _BYTES_PER_SECOND))
    run._finish_delivery(run.current_item)
    await _collect(run)


async def test_the_own_item_report_starts_the_run_and_refines_the_duration(
    tmp_path: Path,
) -> None:
    """The engine reaching the item once the play was sent is what playback start means."""
    run = _make_run(tmp_path, duration=180)
    item = run.current_item
    # the account's previous item is announced before the play command lands
    await run._handle_event(_playback_event("playing", uri=TRACK_B, duration_ms=200_000))
    assert not item.started.is_set()
    assert run._error is None
    run._play_sent = True
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=179_000))
    assert item.started.is_set()
    assert item.engine_duration_ms == 179_000
    assert item.length_ms == 179_000


def test_a_seek_is_confirmed_only_near_its_target(tmp_path: Path) -> None:
    """A pre-seek position report cannot confirm the seek."""
    run = _make_run(tmp_path)
    run._seek_target_ms = 60_000
    run._observe_position(0)
    assert not run._seek_landed.is_set()
    run._observe_position(58_000)
    assert run._seek_landed.is_set()


def test_the_args_run_the_ordinary_mode_without_an_item(tmp_path: Path) -> None:
    """The engine is spawned as a device that is told what to play, never on one URI."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20})
    backend._binary = tmp_path / "soloist-bin"
    args = backend._session_args()
    assert "--single-track" not in args
    assert not any(arg.startswith("spotify:") for arg in args)
    assert "--device-name" in args


async def test_a_run_for_another_item_reports_capacity(tmp_path: Path) -> None:
    """A run held by one stream is the one stream slot: anything else waits or resolves elsewhere."""
    backend = _streamable_backend(tmp_path)
    backend._run = _make_run(tmp_path, uri=TRACK_A, backend=backend)
    with pytest.raises(ProviderStreamLimitError):
        await backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)


async def test_a_replaced_streams_continuation_is_superseded(tmp_path: Path) -> None:
    """A continuation must not take the run back from the stream that replaced it."""
    backend = _streamable_backend(tmp_path)
    streamdetails = _streamdetails_for(uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK)
    backend._run = _make_run(tmp_path, uri=CHAPTER_B, streamdetails=streamdetails, backend=backend)
    with pytest.raises(StreamSupersededError):
        await backend._acquire_item(CHAPTER_A, 0, streamdetails, continuation=True)


async def test_session_normalizes_answers_only_for_the_items_own_run(tmp_path: Path) -> None:
    """Another item's run says nothing about this one; the item it moved on to it answers for."""
    backend = _make_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    run.engine_normalizes = True
    run._engine_normalization_enabled = MagicMock(return_value=True)  # type: ignore[method-assign]
    backend._run = run
    assert backend.session_normalizes(_streamdetails_for(uri=TRACK_A)) is True
    assert backend.session_normalizes(_streamdetails_for(uri=TRACK_B)) is None
    await _feed(run)
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))
    assert run._next is not None
    run._next.bind(_streamdetails_for(uri=TRACK_B))
    assert backend.session_normalizes(_streamdetails_for(uri=TRACK_B)) is True


def test_losing_the_device_mid_item_is_reported_as_a_takeover(tmp_path: Path) -> None:
    """Another player taking the account is what ends this item, and it must say so."""
    run = _live_run(tmp_path)
    run._observe_device_active(active=False)
    assert run._error is not None
    assert "already streaming somewhere else" in run._error


def test_the_device_report_at_startup_is_not_a_takeover(tmp_path: Path) -> None:
    """The engine reports itself inactive while a run is still starting up."""
    run = _make_run(tmp_path)
    _swallow_tasks(run)
    run._observe_device_active(active=False)
    assert run._error is None


def test_losing_the_device_once_the_daemon_has_gone_is_not_a_takeover(tmp_path: Path) -> None:
    """A run whose engine exited sheds the device as it goes; that is no other player."""
    run = _live_run(tmp_path)
    run._engine_exited = True
    run._observe_device_active(active=False)
    assert run._error is None


def test_losing_the_device_after_the_item_is_over_is_not_a_takeover(tmp_path: Path) -> None:
    """With nothing being streamed the run stands down quietly instead of failing."""
    run = _live_run(tmp_path)
    run.current_item.end_at = 0
    run._observe_device_active(active=False)
    assert run._error is None
    cast("MagicMock", run.mass.create_task).assert_called_once()


def _shorten_pause_grace(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink the wait a pause is given to prove external, and return a sleep past it."""
    monkeypatch.setattr(soloist_backend, "_EXTERNAL_PAUSE_GRACE_S", 0.02)
    return 0.1


async def test_a_pause_from_spotify_mid_item_stops_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause from the Spotify app is not a broken track: the queue stops once."""
    past_grace = _shorten_pause_grace(monkeypatch)
    run = _live_run(tmp_path)
    mass = cast("MagicMock", run.mass)
    await run._handle_event(_playback_event("playing"))
    mass.player_queues.stop.assert_not_called()
    await run._handle_event(_playback_event("paused"))
    # the engine repeats the pause in its next snapshot
    await run._handle_event(_playback_event("paused"))
    await asyncio.sleep(past_grace)
    mass.player_queues.stop.assert_called_once_with("player1")
    mass.create_task.assert_called_once()
    assert run._error is None


def _engine_exited(run: _EngineRun) -> None:
    run._engine_exited = True


def _item_ended(run: _EngineRun) -> None:
    run.current_item.end_at = run.current_item.raw_read


def _delivery_done(run: _EngineRun) -> None:
    run.current_item.delivery_done = True


def _stopped(run: _EngineRun) -> None:
    run._stopped = True


@pytest.mark.parametrize("settle", [_engine_exited, _item_ended, _delivery_done])
async def test_the_engine_pausing_at_its_items_end_does_not_stop_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settle: Callable[[_EngineRun], None]
) -> None:
    """A pause that the item's end or the engine's exit overtakes is no listener's pause."""
    past_grace = _shorten_pause_grace(monkeypatch)
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing"))
    await run._handle_event(_playback_event("paused"))
    settle(run)
    await asyncio.sleep(past_grace)
    cast("MagicMock", run.mass).player_queues.stop.assert_not_called()


async def test_a_pause_the_app_takes_back_does_not_stop_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Play pressed again in the Spotify app before the grace ran out leaves the queue alone."""
    past_grace = _shorten_pause_grace(monkeypatch)
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing"))
    await run._handle_event(_playback_event("paused"))
    first_watch = run._pause_watch
    # a resume may land as a rebuffer first
    await run._handle_event(_playback_event("buffering"))
    await asyncio.sleep(past_grace)
    cast("MagicMock", run.mass).player_queues.stop.assert_not_called()
    assert first_watch is not None
    assert first_watch.done()


async def test_a_pause_while_the_engine_buffers_stops_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause straight after a rebuffer still counts."""
    past_grace = _shorten_pause_grace(monkeypatch)
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing"))
    await run._handle_event(_playback_event("buffering"))
    await run._handle_event(_playback_event("paused"))
    await asyncio.sleep(past_grace)
    cast("MagicMock", run.mass).player_queues.stop.assert_called_once_with("player1")


async def test_a_repeated_pause_gets_its_own_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause taken back and repeated is judged on a fresh deadline, not the first one's."""
    _shorten_pause_grace(monkeypatch)
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing"))
    await run._handle_event(_playback_event("paused"))
    first_watch = run._pause_watch
    await run._handle_event(_playback_event("playing"))
    await run._handle_event(_playback_event("paused"))
    await asyncio.sleep(0)
    assert first_watch is not None
    assert first_watch.cancelled()
    assert run._pause_watch is not first_watch


@pytest.mark.parametrize("settle", [_stopped, _item_ended, _engine_exited, _delivery_done])
async def test_a_pause_as_the_run_ends_does_not_stop_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settle: Callable[[_EngineRun], None]
) -> None:
    """The engine settling as its item ends or the run is torn down is no listener's pause."""
    past_grace = _shorten_pause_grace(monkeypatch)
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing"))
    settle(run)
    await run._handle_event(_playback_event("paused"))
    await asyncio.sleep(past_grace)
    cast("MagicMock", run.mass).player_queues.stop.assert_not_called()


async def test_a_pause_before_audio_flows_does_not_stop_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run still starting up (or seeking) has released no audio to pause."""
    past_grace = _shorten_pause_grace(monkeypatch)
    run = _live_run(tmp_path)
    run._reader = None
    await run._handle_event(_playback_event("playing"))
    await run._handle_event(_playback_event("paused"))
    await asyncio.sleep(past_grace)
    cast("MagicMock", run.mass).player_queues.stop.assert_not_called()


async def test_a_rewound_pause_near_the_end_with_nothing_fed_ends_the_item(
    tmp_path: Path,
) -> None:
    """Past the last item it was given the engine pauses at 0:00: the item is over."""
    run = _live_run(tmp_path, duration=180)
    item = run.current_item
    item.raw_read = 171 * _BYTES_PER_SECOND
    await run._handle_event(_playback_event("paused", position_ms=0))
    assert item.end_at == item.raw_read
    # what renders from here on belongs to nobody, and the sink stops rendering it
    await run._route(_audio(0.05))
    assert item.delivery_done
    assert _cushioned(item) == b""
    _sink(run).suspend.assert_awaited()
    # the item's own end is no listener's pause
    assert run._pause_watch is None


async def test_a_pause_at_zero_far_from_the_end_is_not_a_natural_end(tmp_path: Path) -> None:
    """Anywhere but at its end, a pause at 0:00 came from somebody else."""
    run = _live_run(tmp_path, duration=180)
    item = run.current_item
    item.raw_read = 30 * _BYTES_PER_SECOND
    await run._handle_event(_playback_event("playing"))
    await run._handle_event(_playback_event("paused", position_ms=0))
    assert not item.ended
    assert run._pause_watch is not None


async def test_a_wander_with_nothing_fed_ends_the_item_cleanly(tmp_path: Path) -> None:
    """Autoplay moving the engine on past the last item it was given is that item's natural end."""
    run = _live_run(tmp_path, duration=60)
    item = run.current_item
    await _capture(run, _audio(2))
    await run._handle_event(_track_event(TRACK_B))
    assert run._error is None
    assert item.end_at == 2 * _BYTES_PER_SECOND
    # the autoplayed track's audio is not this item's
    await _capture(run, _audio(0.5, 2))
    _sink(run).suspend.assert_awaited()
    assert await _collect(run, item) == _audio(2)


async def test_another_item_while_a_fed_one_is_pending_fails_the_run(tmp_path: Path) -> None:
    """With an item queued on it the engine only goes elsewhere when someone else took over."""
    run = _live_run(tmp_path)
    await _feed(run)
    await run._handle_event(_track_event(TRACK_C))
    assert run._error is not None
    assert "already streaming somewhere else" in run._error
    assert run.current_item.error == run._error


@pytest.mark.parametrize(
    "report",
    [
        # back at the head of its context: the run's first item
        _playback_event("paused", uri=TRACK_A, position_ms=0),
        # back at the head of a context an earlier item started
        _playback_event("paused", uri=TRACK_C, position_ms=0),
        # on to another track
        _track_event(TRACK_C),
    ],
)
async def test_a_fed_track_spotify_refuses_fails_at_once(
    tmp_path: Path, report: SoloistEvent
) -> None:
    """The engine leaving the fed track before any of it played fails that item, not the run."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing, refused = await _reach_fed_item(run)
    _, claimed = await backend._acquire_item(
        TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False
    )
    assert claimed is refused

    await run._handle_event(report)
    assert _upcoming(run) is None
    assert run._error is None
    assert not run.stopped
    with pytest.raises(AudioError, match="would not play"):
        await asyncio.wait_for(_collect(run, refused), 1)
    # the item before it still plays to its end, and what the engine renders after it
    # belongs to nobody
    await _capture(run, _audio(0.5, 2))
    _sink(run).suspend.assert_awaited()
    assert await asyncio.wait_for(_collect(run, outgoing), 1) == _audio(1.5)


async def test_a_refusal_after_the_cut_fails_the_fed_item_too(tmp_path: Path) -> None:
    """The capture can reach the fed track before the engine reports leaving it."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    _, refused = await _reach_fed_item(run)
    await backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)
    # the silence the engine renders before a track's first sample
    await _capture(run, _silence(0.5))
    assert run.current_item is refused

    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    assert refused.ended
    assert run._error is None
    # going back to the head of its context is no pause from the Spotify app
    assert run._pause_watch is None
    with pytest.raises(AudioError, match="would not play"):
        await asyncio.wait_for(_collect(run, refused), 1)
    # what the engine renders from here on is nobody's audio
    suspends = _sink(run).suspend.await_count
    await _capture(run, _silence(0.1))
    assert _sink(run).suspend.await_count == suspends + 1


async def test_a_skip_onto_a_fed_track_spotify_refuses_fails_it(tmp_path: Path) -> None:
    """A skip that lands on the fed track meets the refusal the same way as a boundary."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await _feed(run)
    _client(run).skip_next.side_effect = lambda **_: run._observe_engine_item(
        TRACK_B, announced=True
    )
    item = await run.move_to(TRACK_B, 0, _streamdetails_for(uri=TRACK_B))

    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    assert item.ended
    assert run._error is None
    # not mistaken for a pause from the Spotify app, which would stop the queue
    assert run._pause_watch is None
    assert TRACK_B in backend._refused_until
    with pytest.raises(AudioError, match="would not play"):
        await asyncio.wait_for(_collect(run, item), 1)


async def test_a_refusal_reported_before_the_skip_landed_still_fails_it(tmp_path: Path) -> None:
    """Both reports can reach the run while the skip is still landing."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await _feed(run)

    def _refuse_at_once(**_: Any) -> None:
        run._observe_engine_item(TRACK_B, announced=True)
        run._observe_engine_item(TRACK_A, announced=True)

    _client(run).skip_next.side_effect = _refuse_at_once
    item = await run.move_to(TRACK_B, 0, _streamdetails_for(uri=TRACK_B))
    assert item.ended
    assert TRACK_B in backend._refused_until
    with pytest.raises(AudioError, match="would not play"):
        await asyncio.wait_for(_collect(run, item), 1)


async def test_a_seek_onto_a_fed_track_spotify_refuses_fails_it(tmp_path: Path) -> None:
    """The context head paused at its start can confirm a short seek onto a refused track."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await _feed(run)
    _client(run).skip_next.side_effect = lambda **_: run._observe_engine_item(
        TRACK_B, announced=True
    )

    async def _refuse(*_: Any) -> None:
        await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))

    _client(run).seek.side_effect = _refuse
    item = await run.move_to(TRACK_B, 1000, _streamdetails_for(uri=TRACK_B))
    assert item.ended
    assert TRACK_B in backend._refused_until
    with pytest.raises(AudioError, match="would not play"):
        await asyncio.wait_for(_collect(run, item), 1)


async def test_a_seek_onto_the_fed_track_is_no_refusal_at_its_end(tmp_path: Path) -> None:
    """A confirmed seek proves the engine plays the track, so leaving it later is its end."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await _feed(run)
    _client(run).skip_next.side_effect = lambda **_: run._observe_engine_item(
        TRACK_B, announced=True
    )
    _confirm_seeks(run)
    details = _streamdetails_for(uri=TRACK_B)
    details.duration = 180
    item = await run.move_to(TRACK_B, 179_500, details)

    # its silent last half second is trimmed, then the engine is back at its context head
    await _capture(run, _silence(0.5))
    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    assert item.ended
    assert item.error is None
    assert not run.backend._refused_until
    assert run._pause_watch is None


async def test_a_track_refused_a_moment_ago_is_not_fed(tmp_path: Path) -> None:
    """Its request fails at once, so queueing it would only have the engine play it to nobody."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._note_refusal(TRACK_B)
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_B)
    _client(run).add_to_queue.assert_not_called()
    assert run._fed is None


async def test_the_engine_leaving_a_fed_track_that_played_is_its_end(tmp_path: Path) -> None:
    """Once the fed track's audio arrived, the engine moving on is no refusal."""
    run = _live_run(tmp_path)
    _, upcoming = await _cross_fed_boundary(run)
    await run._handle_event(_track_event(TRACK_C))
    assert upcoming.ended
    assert upcoming.error is None
    assert not run.backend._refused_until


async def test_a_pause_on_the_fed_track_is_no_refusal(tmp_path: Path) -> None:
    """The engine pausing on the fed track itself did not refuse it."""
    run = _live_run(tmp_path)
    _, upcoming = await _reach_fed_item(run)
    await run._handle_event(_playback_event("paused", uri=TRACK_B, position_ms=0))
    assert upcoming.error is None
    assert _upcoming(run) is upcoming


async def test_a_refused_track_fails_at_once_when_asked_for_again(tmp_path: Path) -> None:
    """The speaker reaching the refused track later gets the refusal, not a fresh run on it."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await _reach_fed_item(run)
    await run._handle_event(_track_event(TRACK_C))
    # the run idled out by the time the speaker gets there
    backend._run = None
    backend._start_run = AsyncMock(side_effect=AudioError("served"))  # type: ignore[method-assign]
    details = _streamdetails_for(uri=TRACK_B)

    with pytest.raises(AudioError, match="would not play"):
        await anext(backend.stream_spotify_uri(TRACK_B, streamdetails=details))
    backend._start_run.assert_not_called()
    # any other track is served as usual
    with pytest.raises(AudioError, match="served"):
        await anext(
            backend.stream_spotify_uri(TRACK_C, streamdetails=_streamdetails_for(uri=TRACK_C))
        )
    # Spotify may well play it later: once the memory runs out the track is tried again
    backend._refused_until[TRACK_B] = 0.0
    with pytest.raises(AudioError, match="served"):
        await anext(backend.stream_spotify_uri(TRACK_B, streamdetails=details))


async def test_a_refusal_that_lands_while_its_request_waits_is_not_tried(tmp_path: Path) -> None:
    """A request already waiting for the run when the refusal comes in fails instead of playing it."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await _reach_fed_item(run)
    run.move_to = AsyncMock()  # type: ignore[method-assign]
    backend._start_run = AsyncMock()  # type: ignore[method-assign]
    async with backend._run_lock:
        waiting = asyncio.ensure_future(
            backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)
        )
        await asyncio.sleep(0)
        await run._handle_event(_track_event(TRACK_C))
    with pytest.raises(AudioError, match="would not play"):
        await asyncio.wait_for(waiting, 1)
    run.move_to.assert_not_called()
    backend._start_run.assert_not_called()


async def test_the_item_after_a_refused_one_plays_on_the_running_engine(tmp_path: Path) -> None:
    """The run outlives the refusal, so the queue's next item needs no fresh engine."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing, _ = await _reach_fed_item(run)
    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    await _capture(run, _audio(0.5, 2))
    await backend.release_item(run, outgoing)
    client = _client(run)
    client.play.side_effect = lambda uri, **_: run._observe_engine_item(uri, announced=True)
    backend._start_run = AsyncMock()  # type: ignore[method-assign]

    acquired, item = await backend._acquire_item(
        TRACK_C, 0, _streamdetails_for(uri=TRACK_C), continuation=False
    )
    assert acquired is run
    assert item.spotify_uri == TRACK_C
    client.play.assert_awaited_once_with(TRACK_C)
    backend._start_run.assert_not_called()


async def _read_quiet_fifo(run: _EngineRun, seconds: float) -> None:
    """Run the capture for a while on a FIFO the engine writes nothing more to."""
    run._reader = asyncio.StreamReader()
    reading = asyncio.ensure_future(run._read_capture())
    await asyncio.sleep(seconds)
    run._stopped = True
    await asyncio.wait_for(reading, 1)


async def test_the_item_before_a_refused_one_ends_where_the_paused_engine_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cut the exact length put past the engine's last audio still ends the item at once."""
    monkeypatch.setattr(soloist_backend, "_READ_SLICE_S", 0.01)
    run = _live_run(tmp_path)
    outgoing = run.current_item
    await run._handle_event(
        _playback_event("playing", uri=TRACK_A, position_ms=0, duration_ms=2_000)
    )
    await _reach_fed_item(run)
    # the exact length cuts half a second past where the engine reported the change
    assert outgoing.end_at == 2 * _BYTES_PER_SECOND
    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))

    await _read_quiet_fifo(run, 0.1)
    assert outgoing.delivery_done
    _sink(run).suspend.assert_awaited()
    assert run._error is None
    assert await asyncio.wait_for(_collect(run, outgoing), 1) == _audio(1.5)


async def test_an_item_the_engine_paused_short_of_its_cut_ends_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing fed, the engine pausing at its context head leaves the cut unreached."""
    monkeypatch.setattr(soloist_backend, "_READ_SLICE_S", 0.01)
    run = _live_run(tmp_path)
    item = run.current_item
    await _capture(run, _audio(2))
    item.end_at = item.raw_read + _BYTES_PER_SECOND // 2
    await run._handle_event(_playback_event("paused", uri=TRACK_C, position_ms=0))

    await _read_quiet_fifo(run, 0.1)
    assert item.delivery_done
    assert await asyncio.wait_for(_collect(run, item), 1) == _audio(2)


@pytest.mark.parametrize(
    ("short_s", "status"),
    [
        # further from its cut than an exact length can move it: a stalled engine
        (1.5, "paused"),
        # an engine still going owes the rest of the item
        (0.5, "playing"),
    ],
)
async def test_an_item_short_of_its_cut_is_left_to_the_stall_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_s: float, status: str
) -> None:
    """Only a paused engine a little short of the cut is taken to have nothing more."""
    monkeypatch.setattr(soloist_backend, "_READ_SLICE_S", 0.01)
    monkeypatch.setattr(soloist_backend, "_STALL_TIMEOUT_S", 0.1)
    run = _live_run(tmp_path)
    item = run.current_item
    await _capture(run, _audio(2))
    item.end_at = item.raw_read + int(short_s * _BYTES_PER_SECOND)
    await run._handle_event(_playback_event(status, uri=TRACK_C, position_ms=0))
    run._fail = MagicMock()  # type: ignore[method-assign]

    await _read_quiet_fifo(run, 0.4)
    assert not item.delivery_done
    run._fail.assert_called_once_with("Spotify stopped sending audio")


async def test_a_failed_sign_in_fails_the_run_at_once(tmp_path: Path) -> None:
    """The engine's failed-login line ends the run; the daemon itself only sits there."""
    run = _make_run(tmp_path)
    run._logged_in = False
    create_task = _swallow_tasks(run)
    run.backend.provider.unload_with_error = MagicMock()  # type: ignore[method-assign]
    await run._log_output(_daemon_saying("login failed: make sure --api-key is valid"))
    assert run._error is not None
    assert "could not sign in" in run._error
    # the daemon is put down right away rather than left to sit out the budget
    create_task.assert_called_once()
    # a sign-in turned down once is not a lost pairing: the item fails, the
    # provider stays up and the next track signs in afresh
    with pytest.raises(AudioError, match="could not sign in"):
        run._raise_startup_error("did not start up correctly")
    run.backend.provider.unload_with_error.assert_not_called()


async def test_a_login_complaint_once_the_item_plays_is_left_to_the_engine(
    tmp_path: Path,
) -> None:
    """An item that started has audio to lose; a later login complaint is the engine's."""
    run = _make_run(tmp_path)
    run.current_item.started.set()
    await run._log_output(_daemon_saying("login failed: make sure --api-key is valid"))
    assert run._error is None


def test_a_sign_in_that_never_arrived_is_not_a_lost_pairing(tmp_path: Path) -> None:
    """Timing out on the login fails the item and leaves the provider in service."""
    run = _make_run(tmp_path)
    run._logged_in = False
    run.backend.provider.unload_with_error = MagicMock()  # type: ignore[method-assign]
    with pytest.raises(AudioError, match="could not connect and sign in"):
        run._raise_startup_error("could not connect and sign in")
    run.backend.provider.unload_with_error.assert_not_called()


async def test_a_confirmed_lost_pairing_sends_the_user_back_through_setup(
    tmp_path: Path,
) -> None:
    """The engine advertising itself for pairing, with no stored session, points at setup."""
    run = _make_run(tmp_path)
    run._logged_in = False
    _swallow_tasks(run)
    run.backend.provider.unload_with_error = MagicMock()  # type: ignore[method-assign]
    # the data directory under tmp_path holds no session to confirm against
    await run._log_output(
        _daemon_saying('waiting for login - connect to "Music Assistant" from your Spotify app')
    )
    assert run._unpaired is True
    with pytest.raises(LoginFailed) as err:
        run._raise_startup_error("did not start up correctly")
    assert err.value.translation_key == "soloist_pairing_required"
    run.backend.provider.unload_with_error.assert_called_once()


def _confirm_seeks(run: _EngineRun) -> None:
    """Have the mocked engine report every seek as landed."""
    _client(run).seek.side_effect = lambda position_ms, **_: run._observe_position(position_ms)


async def test_a_seek_moves_the_engine_once_the_held_stream_let_go(tmp_path: Path) -> None:
    """A positive seek waits for the stream it replaces, then seeks the running engine in place."""
    backend = _streamable_backend(tmp_path)
    held = _live_run(tmp_path, backend=backend)
    await held._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=180_000))
    _confirm_seeks(held)
    backend._run = held
    previous = held.current_item

    async def _released(run: _EngineRun, *_args: Any) -> None:
        await backend.release_item(run, previous)

    with patch.object(backend, "_wait_run_released", side_effect=_released):
        run, item = await backend._acquire_item(
            TRACK_A, 30, _streamdetails_for(uri=TRACK_A), continuation=False
        )
    assert run is held
    assert backend._run is held
    assert item is not previous
    assert item.seek_target_ms == 30_000
    _client(held).seek.assert_awaited_with(30_000)
    _client(held).play.assert_not_called()
    # the exact length the engine reported earlier carries over to the new item
    assert item.engine_duration_ms == 180_000


async def test_a_seek_never_steals_a_held_item(tmp_path: Path) -> None:
    """A second queue occurrence of the same track must not cut its playing twin."""
    backend = _streamable_backend(tmp_path)
    held = _live_run(tmp_path, backend=backend)
    backend._run = held
    with (
        patch.object(backend, "_wait_run_released", AsyncMock()),
        pytest.raises(ProviderStreamLimitError),
    ):
        await backend._acquire_item(
            TRACK_A, 30, _streamdetails_for(uri=TRACK_A), continuation=False
        )
    assert held.current_item.streaming


async def test_a_held_run_whose_teardown_was_cut_short_is_reclaimed(tmp_path: Path) -> None:
    """A stopped run left behind finishes its teardown instead of reporting capacity."""
    backend = _streamable_backend(tmp_path)
    held = _make_run(tmp_path, uri=TRACK_A, backend=backend)
    held._stopped = True
    held.stop = AsyncMock()  # type: ignore[method-assign]
    backend._run = held

    with (
        patch(
            "music_assistant.providers.spotify.backends.soloist.SoloistBinaryManager.ensure_fresh",
            AsyncMock(),
        ),
        patch.object(soloist_backend._EngineRun, "start", AsyncMock()),
    ):
        run, item = await backend._acquire_item(
            TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False
        )
    held.stop.assert_awaited_once()
    assert run is not held
    assert backend._run is run
    assert item is run.current_item


async def test_a_failed_move_falls_back_to_a_fresh_run(tmp_path: Path) -> None:
    """An engine that will not go where it was asked is replaced by one started on the item."""
    backend = _streamable_backend(tmp_path)
    idle = _live_run(tmp_path, backend=backend)
    idle.current_item.streaming = False
    idle.move_to = AsyncMock(side_effect=AudioError("did not land"))  # type: ignore[method-assign]
    idle.stop = AsyncMock()  # type: ignore[method-assign]
    backend._run = idle

    with (
        patch(
            "music_assistant.providers.spotify.backends.soloist.SoloistBinaryManager.ensure_fresh",
            AsyncMock(),
        ),
        patch.object(soloist_backend._EngineRun, "start", AsyncMock()),
    ):
        run, item = await backend._acquire_item(
            TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False
        )
    idle.move_to.assert_awaited_once()
    idle.stop.assert_awaited_once()
    assert run is not idle
    assert backend._run is run
    assert item is run.current_item
    assert item.spotify_uri == TRACK_B


async def test_a_stream_cancelled_again_during_its_release_frees_the_run(
    tmp_path: Path,
) -> None:
    """
    A second cancel while the stream releases its item must not leave the run held.

    The release runs as its own task, so the second cancel finds nothing to interrupt
    and the run is dropped once that task finishes.
    """
    backend = _streamable_backend(tmp_path)
    run = _make_run(tmp_path, uri=TRACK_A, backend=backend)
    item = run.current_item
    backend._run = run
    release_started = asyncio.Event()
    finish_release = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []

    async def _release(_item: _Item) -> None:
        release_started.set()
        await finish_release.wait()
        # nothing else holds the run: it is to be torn down
        run._stopped = True

    async def _stream(_item: _Item) -> AsyncGenerator[bytes]:
        yield b"\x00" * 64
        await asyncio.Event().wait()

    def _create_task(coro: Any) -> asyncio.Task[Any]:
        tasks.append(task := asyncio.get_running_loop().create_task(coro))
        return task

    run.release = _release  # type: ignore[method-assign,assignment]
    run.stream = _stream  # type: ignore[method-assign,assignment]
    run.stop = AsyncMock()  # type: ignore[method-assign]
    first_chunk = asyncio.Event()

    async def _consume() -> None:
        async for _chunk in backend.stream_spotify_uri(TRACK_A):
            first_chunk.set()

    with (
        patch.object(backend, "_acquire_item", AsyncMock(return_value=(run, item))),
        patch.object(backend.mass, "create_task", side_effect=_create_task),
    ):
        consumer = asyncio.create_task(_consume())
        await first_chunk.wait()
        consumer.cancel()
        await release_started.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        # released only once its release is through, never by the cancel itself
        assert backend._run is run
        finish_release.set()
        await asyncio.gather(*tasks)
    assert backend._run is None


async def test_the_startup_resets_the_play_options_before_playing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device is claimed and the account's shuffle and repeat switched off before the play."""
    backend = _streamable_backend(tmp_path)
    run = _EngineRun(backend, TRACK_A, 0, _streamdetails_for(uri=TRACK_A))
    handlers: list[Callable[[SoloistEvent], Any]] = []

    async def _listen(handler: Callable[[SoloistEvent], Any]) -> None:
        handlers.append(handler)
        await handler(
            SoloistEvent(
                type="auth_state", data=SoloistAuthState(logged_in=True, is_active=True), raw={}
            )
        )
        await asyncio.Event().wait()

    async def _play(uri: str | None = None, **_kwargs: Any) -> None:
        await handlers[0](_playback_event("playing", uri=uri, position_ms=0))

    client = AsyncMock()
    client.connected = True
    client.wait_until_ready = AsyncMock(return_value=True)
    client.listen_events = _listen
    client.play.side_effect = _play

    async def _hang() -> None:
        await asyncio.Event().wait()

    async def _no_output() -> AsyncGenerator[str]:
        lines: tuple[str, ...] = ()
        for line in lines:
            yield line

    proc = MagicMock(returncode=None, iter_stdout=_no_output)
    proc.start = AsyncMock()
    proc.wait = AsyncMock(side_effect=_hang)
    proc.close = AsyncMock()
    sink = AsyncMock()
    monkeypatch.setattr(soloist_backend, "SoloistClient", MagicMock(return_value=client))
    monkeypatch.setattr(soloist_backend, "AsyncProcess", MagicMock(return_value=proc))
    monkeypatch.setattr(soloist_backend, "PipeSink", MagicMock(create=AsyncMock(return_value=sink)))
    monkeypatch.setattr(
        soloist_backend,
        "_open_fifo_reader",
        AsyncMock(return_value=(asyncio.StreamReader(), None)),
    )
    try:
        async with asyncio.timeout(5):
            await run.start()
        commands = [
            entry
            for entry in client.mock_calls
            if entry[0]
            in (
                "activate",
                "set_shuffle",
                "set_repeat_context",
                "set_repeat_track",
                "play",
                "get_queue",
            )
        ]
        assert commands == [
            call.activate(await_result=True),
            call.set_shuffle(False, await_result=True),
            call.set_repeat_context(False, await_result=True),
            call.set_repeat_track(False, await_result=True),
            call.play(TRACK_A),
            # the account's own queued items decide whether items can be fed
            call.get_queue(),
        ]
        assert run.current_item.started.is_set()
        sink.resume.assert_awaited()
    finally:
        await run.stop()


async def test_a_fed_item_takes_over_at_the_exact_engine_length(tmp_path: Path) -> None:
    """
    The engine reaching the fed item cuts the capture where the outgoing one's audio ends.

    The remainder goes to the next item, which the request for its URI takes up,
    so each queue item is served to its own StreamDetails.
    """
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing = run.current_item
    details_a, details_b = _streamdetails_for(uri=TRACK_A), _streamdetails_for(uri=TRACK_B)

    await backend.feed_next(details_a.uri, TRACK_B)
    _client(run).add_to_queue.assert_awaited_once_with(TRACK_B)
    await run._handle_event(_queue_event(("q1", TRACK_B)))
    await run._handle_event(
        _playback_event("playing", uri=TRACK_A, position_ms=0, duration_ms=2_000)
    )
    await _capture(run, _audio(1.5))
    assert _upcoming(run) is None

    # the queued entry leaving the queue is the engine moving on, reported late
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))
    assert run._fed is None
    assert run._next is not None
    assert run._next.spotify_uri == TRACK_B
    assert outgoing.end_at == 2 * _BYTES_PER_SECOND

    await _capture(run, _audio(0.5, 2) + _audio(0.5, 3))
    upcoming = run.current_item
    assert upcoming.spotify_uri == TRACK_B
    assert upcoming.media_key is None
    # the item before it still streams: the claim is not due yet
    assert upcoming.unclaimed_since is None

    claimed_run, claimed = await backend._acquire_item(TRACK_B, 0, details_b, continuation=False)
    assert claimed_run is run
    assert claimed is upcoming
    assert outgoing.media_key == details_a.uri
    assert claimed.media_key == details_b.uri
    assert await _collect(run, outgoing) == _audio(1.5) + _audio(0.5, 2)
    assert _cushioned(claimed) == _audio(0.5, 3)


@pytest.mark.parametrize(
    ("engine_duration_ms", "expected_s"),
    [
        # the exact length, slightly past where the report came in
        (2_000, 2.0),
        # unknown: the reported moment is all there is
        (None, 1.5),
        # already passed: the cut lands where the capture stands
        (1_000, 1.5),
        # too far from the report to be the same moment
        (10_000, 1.5),
    ],
)
def test_the_cut_falls_back_to_the_reported_offset(
    engine_duration_ms: int | None, expected_s: float
) -> None:
    """Only a plausible exact length beats the moment the engine reported the change."""
    item = _Item(TRACK_A, 0, None, max_lead_trim_s=_MAX_LEAD_TRIM_S)
    item.engine_duration_ms = engine_duration_ms
    item.raw_read = int(1.5 * _BYTES_PER_SECOND)
    assert item.end_offset(item.raw_read) == int(expected_s * _BYTES_PER_SECOND)


def test_a_seeked_items_cut_counts_the_rest_of_it() -> None:
    """An item started part-way ends after what was left of it."""
    item = _Item(TRACK_A, 60_000, None, max_lead_trim_s=_MAX_LEAD_TRIM_S)
    item.engine_duration_ms = 62_000
    item.raw_read = int(1.5 * _BYTES_PER_SECOND)
    assert item.end_offset(item.raw_read) == 2 * _BYTES_PER_SECOND


async def test_the_same_track_twice_is_told_by_its_restart(tmp_path: Path) -> None:
    """The engine reports no change of track when it plays the same one again, only a restart."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    first = run.current_item
    await _feed(run, TRACK_A)
    _client(run).add_to_queue.assert_awaited_once_with(TRACK_A)
    await _capture(run, _audio(1))
    await run._handle_event(_track_event(TRACK_A))
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=1_000))
    assert _upcoming(run) is None
    assert not first.ended

    await run._handle_event(_queue_event())
    # the entry left the queue, but only the restart says the engine started it
    assert run.current_item is first
    assert not first.ended
    await run._handle_event(_playback_event("playing", uri=TRACK_A, position_ms=17))
    assert run._next is not None
    assert run._next.spotify_uri == TRACK_A
    assert first.end_at == first.raw_read
    # the second occurrence asks with the same details and takes the next item
    _, second = await backend._acquire_item(
        TRACK_A, 0, _streamdetails_for(uri=TRACK_A), continuation=False
    )
    assert second is run._next
    assert second is not first
    await _capture(run, _audio(0.5, 2))
    assert run.current_item is second
    assert _cushioned(second) == _audio(0.5, 2)


async def test_a_late_restart_report_still_cuts_at_the_exact_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart reported seconds in is dated back to where the repeat started."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=1_000))
    first = run.current_item
    await _feed(run, TRACK_A)
    await _capture(run, _audio(0.9))
    # the engine renders ahead of the capture: the FIFO holds the repeat's start
    monkeypatch.setattr(
        soloist_backend, "_unread_fifo_bytes", lambda _: 2 * soloist_backend._BYTES_PER_SECOND
    )
    await run._handle_event(_queue_event())
    await run._handle_event(_playback_event("playing", uri=TRACK_A, position_ms=2_000))
    assert run._next is not None
    assert first.end_at == soloist_backend._BYTES_PER_SECOND


@pytest.mark.parametrize(
    ("media_key", "uri"),
    [
        # the engine does not take episodes into its queue
        (_streamdetails_for(uri=TRACK_A).uri, "spotify:episode:eee"),
        # the hint is about an item this run is not streaming
        (_streamdetails_for(uri=TRACK_C).uri, TRACK_B),
    ],
)
async def test_feeding_is_a_no_op_for(tmp_path: Path, media_key: str, uri: str) -> None:
    """Only a track following the item being streamed is queued on the engine."""
    run = _live_run(tmp_path)
    await run.feed_next(media_key, uri)
    _client(run).add_to_queue.assert_not_called()
    assert run._fed is None


async def test_the_engine_is_fed_once_per_item(tmp_path: Path) -> None:
    """A repeated hint, or one for another item once one is queued, queues nothing more."""
    run = _live_run(tmp_path)
    media_key = cast("str", run.current_item.media_key)
    await run.feed_next(media_key, TRACK_B)
    await run.feed_next(media_key, TRACK_B)
    await run.feed_next(media_key, TRACK_C)
    _client(run).add_to_queue.assert_awaited_once_with(TRACK_B)
    assert run._fed is not None
    assert run._fed.spotify_uri == TRACK_B


async def test_feeding_without_a_run_is_a_no_op(tmp_path: Path) -> None:
    """A hint that arrives when no session runs has nothing to line up."""
    backend = _make_backend(tmp_path)
    await backend.feed_next(_streamdetails_for(uri=TRACK_A).uri, TRACK_B)
    assert backend._run is None


async def _hold(run: _EngineRun, uri: str = TRACK_B) -> None:
    """Hint an item while the current one, 2 s longer than the feed lead, has just started."""
    length_ms = int((_FEED_LEAD_S + 2) * 1000)
    await run._handle_event(
        _playback_event("playing", uri=TRACK_A, position_ms=0, duration_ms=length_ms)
    )
    await run.feed_next(cast("str", run.current_item.media_key), uri)


async def _run_feeds(run: _EngineRun) -> None:
    """Let the feeds the capture scheduled reach the engine."""
    await asyncio.gather(*run._tasks)


async def test_a_hint_is_held_until_the_items_last_seconds(tmp_path: Path) -> None:
    """Queued earlier, the account's own Spotify crossfade would blend the two tracks."""
    run = _live_run(tmp_path)
    await _hold(run)
    await _capture(run, _audio(1.9))
    await _run_feeds(run)
    _client(run).add_to_queue.assert_not_called()
    assert _fed(run) is None

    await _capture(run, _audio(0.2))
    await _run_feeds(run)
    _client(run).add_to_queue.assert_awaited_once_with(TRACK_B)
    assert run._fed is not None
    assert run._fed.spotify_uri == TRACK_B


async def test_a_hint_for_another_item_replaces_the_held_one(tmp_path: Path) -> None:
    """The queue changed before the engine was given anything: the latest hint counts."""
    run = _live_run(tmp_path)
    await _hold(run)
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_C)
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    _client(run).add_to_queue.assert_awaited_once_with(TRACK_C)


async def test_a_hint_the_engine_cannot_take_drops_the_held_one(tmp_path: Path) -> None:
    """The held track is no longer next, so the engine is given nothing."""
    run = _live_run(tmp_path)
    await _hold(run)
    await run.feed_next(cast("str", run.current_item.media_key), "spotify:episode:eee")
    assert run._pending_feed is None
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    _client(run).add_to_queue.assert_not_called()


async def test_a_seek_back_out_of_the_window_takes_the_fed_item_off_the_engine(
    tmp_path: Path,
) -> None:
    """Left queued that far ahead, it would be crossfaded into; the next hint holds it anew."""
    run = _live_run(tmp_path)
    await _hold(run)
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    await run._handle_event(_queue_event(("q1", TRACK_B)))
    client = _client(run)
    client.skip_next.side_effect = lambda **_: run._observe_engine_item(TRACK_B, announced=True)
    client.play.side_effect = lambda played, **_: run._observe_engine_item(played, announced=True)

    item = await run.move_to(TRACK_A, 0, _streamdetails_for(uri=TRACK_A))
    moves = [entry for entry in client.mock_calls if entry[0] in ("skip_next", "play", "seek")]
    assert moves == [call.skip_next(), call.play(TRACK_A)]
    assert run._fed is None
    assert run.current_item is item
    assert not item.fed

    await run._handle_event(_queue_event())
    await run.feed_next(cast("str", item.media_key), TRACK_B)
    assert run._pending_feed == TRACK_B
    client.add_to_queue.assert_awaited_once()


async def test_a_seek_back_keeps_the_length_the_skipped_track_overwrote(tmp_path: Path) -> None:
    """The skipped track's report must not leave the replayed one without a length."""
    run = _live_run(tmp_path)
    await _hold(run)
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    await run._handle_event(_queue_event(("q1", TRACK_B)))
    client = _client(run)

    async def skip(**_: Any) -> None:
        await run._handle_event(
            _playback_event("playing", uri=TRACK_B, position_ms=0, duration_ms=200_000)
        )

    client.skip_next.side_effect = skip
    client.play.side_effect = lambda played, **_: run._observe_engine_item(played, announced=True)
    _confirm_seeks(run)
    item = await run.move_to(TRACK_A, 0, _streamdetails_for(uri=TRACK_A))
    assert item.engine_duration_ms == int((_FEED_LEAD_S + 2) * 1000)

    # fed again in its last seconds and seeked back again before the engine described it
    await run._handle_event(_queue_event())
    await run.feed_next(cast("str", item.media_key), TRACK_B)
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    await run._handle_event(_queue_event(("q2", TRACK_B)))
    await run.move_to(TRACK_A, 0, _streamdetails_for(uri=TRACK_A))
    assert client.add_to_queue.await_count == 2
    assert client.skip_next.await_count == 2


async def test_a_seek_back_after_the_app_removed_the_fed_item_stays_in_place(
    tmp_path: Path,
) -> None:
    """With the entry gone from the engine's queue there is nothing to skip onto."""
    run = _live_run(tmp_path)
    await _hold(run)
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    await run._handle_event(_queue_event(("q1", TRACK_B)))
    # removed in the Spotify app: the entry leaves while the engine stays on the track
    await run._handle_event(_queue_event())
    _confirm_seeks(run)
    client = _client(run)

    await run.move_to(TRACK_A, 0, _streamdetails_for(uri=TRACK_A))
    client.skip_next.assert_not_called()
    client.play.assert_not_called()
    client.seek.assert_awaited_with(0)


async def test_a_seek_back_before_the_queue_report_leaves_the_fed_item_tracked(
    tmp_path: Path,
) -> None:
    """An entry the engine has not confirmed yet cannot be skipped onto, so it is kept."""
    run = _live_run(tmp_path)
    await _hold(run)
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    _confirm_seeks(run)
    client = _client(run)

    await run.move_to(TRACK_A, 1_000, _streamdetails_for(uri=TRACK_A))
    client.skip_next.assert_not_called()
    client.play.assert_not_called()
    assert run._fed is not None
    assert run._fed.spotify_uri == TRACK_B


@pytest.mark.parametrize(
    "length_ms",
    [
        # the hint comes in the item's last seconds
        int((_FEED_LEAD_S + 1) * 1000),
        # the engine has not reported the exact length
        None,
    ],
)
async def test_a_hint_is_fed_at_once(tmp_path: Path, length_ms: int | None) -> None:
    """With the end close or of unknown distance, nothing is held back."""
    run = _live_run(tmp_path)
    await run._handle_event(
        _playback_event("playing", uri=TRACK_A, position_ms=0, duration_ms=length_ms)
    )
    await _capture(run, _audio(1.5))
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_B)
    _client(run).add_to_queue.assert_awaited_once_with(TRACK_B)
    assert run._pending_feed is None


async def test_a_hint_after_a_seek_into_the_last_seconds_is_fed_at_once(tmp_path: Path) -> None:
    """The seeked item starts inside the window."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=180_000))
    _confirm_seeks(run)
    seek_ms = 180_000 - int(_FEED_LEAD_S * 1000)
    item = await run.move_to(TRACK_A, seek_ms, _streamdetails_for(uri=TRACK_A))
    await run.feed_next(cast("str", item.media_key), TRACK_B)
    _client(run).add_to_queue.assert_awaited_once_with(TRACK_B)


async def test_the_held_item_is_dropped_when_the_item_ends(tmp_path: Path) -> None:
    """With nothing queued the engine ran past the item, which is over."""
    run = _live_run(tmp_path)
    await _hold(run)
    await _capture(run, _audio(1))
    await run._handle_event(_track_event(TRACK_C))
    assert run.current_item.ended
    assert run._pending_feed is None
    await _capture(run, _audio(1.5))
    await _run_feeds(run)
    _client(run).add_to_queue.assert_not_called()


@pytest.mark.parametrize(
    ("uri", "seek_ms"),
    [
        # a skip or jump to another track
        (TRACK_C, 0),
        # a seek within the item: its stream hints again
        (TRACK_A, 30_000),
    ],
)
async def test_the_held_item_is_dropped_when_the_run_moves(
    tmp_path: Path, uri: str, seek_ms: int
) -> None:
    """A move leaves nothing queued on the engine to skip past or to crossfade into."""
    run = _live_run(tmp_path)
    await _hold(run)
    client = _client(run)
    client.play.side_effect = lambda played, **_: run._observe_engine_item(played, announced=True)
    _confirm_seeks(run)

    await run.move_to(uri, seek_ms, _streamdetails_for(uri=uri))
    assert run._pending_feed is None
    client.skip_next.assert_not_called()
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    client.add_to_queue.assert_not_called()


@pytest.mark.parametrize(
    ("uri", "held"),
    [
        # another track follows something else
        (TRACK_C, None),
        # the same item seeked back out of its last seconds holds it again
        (TRACK_A, TRACK_B),
    ],
)
async def test_a_feed_waiting_for_the_run_lock_follows_a_move_that_came_first(
    tmp_path: Path, uri: str, held: str | None
) -> None:
    """A move that held the lock first decides what the waiting feed still does."""
    run = _live_run(tmp_path)
    await _hold(run)
    client = _client(run)
    client.play.side_effect = lambda played, **_: run._observe_engine_item(played, announced=True)
    _confirm_seeks(run)
    async with run.backend._run_lock:
        await _capture(run, _audio(2.1))
        await asyncio.sleep(0)
        await run.move_to(uri, 0, _streamdetails_for(uri=uri))
    await _run_feeds(run)
    client.add_to_queue.assert_not_called()
    assert run._pending_feed == held


async def test_a_feed_waiting_for_the_run_lock_dies_with_the_run(tmp_path: Path) -> None:
    """The run stops while its feed waits for the lock: nothing reaches the engine."""
    run = _live_run(tmp_path)
    await _hold(run)
    async with run.backend._run_lock:
        await _capture(run, _audio(2.1))
        await asyncio.sleep(0)
        assert run._tasks
        await run.stop()
    _client(run).add_to_queue.assert_not_called()


async def test_a_late_fed_track_spotify_refuses_fails_at_once(tmp_path: Path) -> None:
    """A held item queued in the last seconds meets a refusal like any fed item."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await _hold(run)
    await _capture(run, _audio(2.1))
    await _run_feeds(run)
    await run._handle_event(_queue_event(("q1", TRACK_B)))
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))
    refused = _upcoming(run)
    assert refused is not None
    await backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)

    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    assert _upcoming(run) is None
    assert run._error is None
    with pytest.raises(AudioError, match="would not play"):
        await asyncio.wait_for(_collect(run, refused), 1)


async def test_a_refusal_drops_the_item_held_to_follow_the_refused_one(tmp_path: Path) -> None:
    """The item Spotify refused is over, so nothing is queued to follow it."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    _, refused = await _reach_fed_item(run)
    await backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)
    # the silence the engine renders before a track's first sample
    await _capture(run, _silence(0.5))
    assert run.current_item is refused
    await run._handle_event(
        _playback_event("playing", uri=TRACK_B, position_ms=0, duration_ms=180_000)
    )
    await run.feed_next(cast("str", refused.media_key), TRACK_C)
    assert run._pending_feed == TRACK_C

    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    assert refused.error is not None
    assert run._pending_feed is None


async def test_a_stop_before_the_fed_item_idles_the_run_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine is held for a moment for what may follow, then stopped instead of playing on."""
    monkeypatch.setattr(soloist_backend, "_IDLE_GRACE_S", 0.01)
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    tasks = _run_tasks(run)
    await _feed(run)
    await _capture(run, _audio(1))

    await backend.release_item(run, run.current_item)
    assert run.current_item.abandoned
    _sink(run).suspend.assert_awaited()
    assert _idle_watch(run) is not None
    assert not run._stopped

    async with asyncio.timeout(5):
        while not tasks:
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)
    assert run.stopped
    assert backend._run is None


async def test_a_run_released_while_its_queue_plays_waits_longer_for_its_next_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that dropped a track asks for the next one; the engine stays for it a while."""
    monkeypatch.setattr(soloist_backend, "_IDLE_GRACE_S", 0.01)
    monkeypatch.setattr(soloist_backend, "_QUEUE_PLAYING_GRACE_S", 0.3)
    backend = _streamable_backend(tmp_path)
    queues = cast("MagicMock", backend.mass.player_queues)
    queues.get.return_value = SimpleNamespace(state=PlaybackState.PLAYING)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    tasks = _run_tasks(run)

    await backend.release_item(run, run.current_item)
    await asyncio.sleep(0.1)
    assert not tasks
    assert not run._stopped
    queues.get.assert_called_with("player1")

    # nothing came for it after all: it still stops rather than playing on to nobody
    async with asyncio.timeout(5):
        while not tasks:
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)
    assert run.stopped
    assert backend._run is None


async def test_a_seek_keeps_the_fed_item(tmp_path: Path) -> None:
    """A seek within the item leaves the item queued after it in place."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=8_000))
    await _feed(run)
    _confirm_seeks(run)
    previous = run.current_item

    item = await run.move_to(TRACK_A, 3_000, _streamdetails_for(uri=TRACK_A))
    client = _client(run)
    client.skip_next.assert_not_called()
    client.play.assert_not_called()
    client.seek.assert_awaited_with(3_000)
    assert run._fed is not None
    assert run._fed.uid == "q1"
    assert run.current_item is item
    assert previous.abandoned
    assert item.engine_duration_ms == 8_000


async def test_a_seek_keeps_the_same_track_fed_after_itself(tmp_path: Path) -> None:
    """A seek announcing the current track leaves its queued repeat for the real boundary."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=8_000))
    await _feed(run, TRACK_A)

    async def seek(position_ms: int, **_: Any) -> None:
        await run._handle_event(_playback_event("playing", uri=TRACK_A, position_ms=position_ms))

    _client(run).seek.side_effect = seek
    item = await run.move_to(TRACK_A, 3_000, _streamdetails_for(uri=TRACK_A))
    assert run._fed is not None
    assert run._fed.uid == "q1"

    await _capture(run, _audio(1))
    await run._handle_event(_queue_event())
    await run._handle_event(_playback_event("playing", uri=TRACK_A, position_ms=17))
    assert item.ended
    assert run._next is not None
    assert run._next.spotify_uri == TRACK_A


async def test_a_jump_elsewhere_consumes_the_fed_item_first(tmp_path: Path) -> None:
    """A queued item survives a jump, so the engine is skipped past it before the play."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await _feed(run)
    client = _client(run)
    client.skip_next.side_effect = lambda **_: run._observe_engine_item(TRACK_B, announced=True)
    client.play.side_effect = lambda uri, **_: run._observe_engine_item(uri, announced=True)

    item = await run.move_to(TRACK_C, 0, _streamdetails_for(uri=TRACK_C))
    moves = [entry for entry in client.mock_calls if entry[0] in ("skip_next", "play")]
    assert moves == [call.skip_next(), call.play(TRACK_C)]
    client.seek.assert_not_called()
    assert run._fed is None
    assert run._next is None
    assert run.current_item is item
    assert item.spotify_uri == TRACK_C


async def test_a_skip_to_the_fed_item_only_skips(tmp_path: Path) -> None:
    """The item queued on the engine is reached by skipping to it, not by playing it afresh."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await _feed(run)
    client = _client(run)
    client.skip_next.side_effect = lambda **_: run._observe_engine_item(TRACK_B, announced=True)

    item = await run.move_to(TRACK_B, 0, _streamdetails_for(uri=TRACK_B))
    client.skip_next.assert_awaited_once()
    client.play.assert_not_called()
    client.seek.assert_not_called()
    assert run._fed is None
    assert item.spotify_uri == TRACK_B
    assert item.media_key == _streamdetails_for(uri=TRACK_B).uri


async def test_an_unclaimed_next_item_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the queue went elsewhere the engine must not play on to nobody."""
    monkeypatch.setattr(soloist_backend, "_UNCLAIMED_ITEM_S", 0.0)
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing, upcoming = await _cross_fed_boundary(run)
    assert upcoming.media_key is None
    # the clock only runs once the item before it is done: the claim follows that
    assert upcoming.unclaimed_since is None
    tasks = _run_tasks(run)
    await backend.release_item(run, outgoing)
    assert _unclaimed_since(upcoming) is not None
    # the watch fires without waiting on a full cushion
    for _ in range(50):
        if run.stopped:
            break
        await asyncio.sleep(0.01)
    await asyncio.gather(*tasks, return_exceptions=True)
    assert run.stopped
    assert backend._run is None


async def test_releasing_the_outgoing_item_keeps_the_run_for_the_next(tmp_path: Path) -> None:
    """The item the engine left draining to its end is no reason to stop the one playing."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing, upcoming = await _cross_fed_boundary(run)
    await backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)
    suspends = _sink(run).suspend.await_count

    await backend.release_item(run, outgoing)
    assert not outgoing.abandoned
    assert run._idle_watch is None
    assert _sink(run).suspend.await_count == suspends
    assert not run.stopped
    assert backend._run is run
    assert run.current_item is upcoming


async def test_a_claim_after_the_outgoing_item_was_let_go_runs_the_capture_on(
    tmp_path: Path,
) -> None:
    """With its predecessor let go short of its end, the next item restarts the idled capture."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing = run.current_item
    await _feed(run)
    await run._handle_event(
        _playback_event("playing", uri=TRACK_A, position_ms=0, duration_ms=2_000)
    )
    await _capture(run, _audio(1.5))
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))

    await backend.release_item(run, outgoing)
    assert outgoing.abandoned
    assert _idle_watch(run) is not None
    _sink(run).suspend.assert_awaited()

    _, upcoming = await backend._acquire_item(
        TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False
    )
    assert upcoming is run._next
    assert run._idle_watch is None
    _sink(run).resume.assert_awaited()
    await _capture(run, _audio(0.5, 2) + _audio(0.5, 3))
    assert run.current_item is upcoming
    assert _cushioned(upcoming) == _audio(0.5, 3)


async def test_a_switched_item_drops_at_most_the_switch_trim(tmp_path: Path) -> None:
    """The silence the engine renders before a switched-to track is trimmed, within its budget."""
    run = _live_run(tmp_path)
    assert run.current_item.shaper._max_lead_trim == int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND)
    await _feed(run)
    await _capture(run, _audio(1.5))
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))
    upcoming = run._next
    assert upcoming is not None
    assert upcoming.shaper._max_lead_trim == int(_MAX_SWITCH_TRIM_S * _BYTES_PER_SECOND)

    await _capture(run, _silence(2) + _audio(1))
    assert run.current_item is upcoming
    budget = int(_MAX_SWITCH_TRIM_S * _BYTES_PER_SECOND)
    assert upcoming.shaper.lead_skipped == budget
    # silence beyond the budget is content
    assert _cushioned(upcoming) == _silence(2)[budget:] + _audio(1)


async def test_a_warm_play_gets_the_exact_cut_counted_from_its_first_sample(
    tmp_path: Path,
) -> None:
    """
    The exact length reported while the engine switches carries over to the item played.

    The engine reports it before the item exists, and the cut then counts from the
    item's first sample, after the silence the switch rendered.
    """
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    client = _client(run)

    async def _play(uri: str | None = None, **_kwargs: Any) -> None:
        await run._handle_event(
            _playback_event("playing", uri=uri, position_ms=0, duration_ms=2_000)
        )

    client.play.side_effect = _play
    item = await run.move_to(TRACK_C, 0, _streamdetails_for(uri=TRACK_C))
    assert item.engine_duration_ms == 2_000

    await _capture(run, _silence(0.5) + _audio(1.2))
    lead = int(0.5 * _BYTES_PER_SECOND)
    assert item.shaper.lead_skipped == lead
    # autoplay moves on with nothing fed, reported 0.8 s before the exact end
    await run._handle_event(_track_event(TRACK_B))
    assert item.end_at == lead + 2 * _BYTES_PER_SECOND
    await _capture(run, _audio(1, 2))
    assert _cushioned(item) == _audio(1.2) + _audio(0.8, 2)


async def test_a_fed_item_gets_the_exact_length_reported_on_reaching_it(tmp_path: Path) -> None:
    """A playback state naming the fed item hands the item it becomes the exact length."""
    run = _live_run(tmp_path)
    await _feed(run)
    await _capture(run, _audio(1))
    await run._handle_event(
        _playback_event("playing", uri=TRACK_B, position_ms=0, duration_ms=3_000)
    )
    assert run._fed is None
    assert run._next is not None
    assert run._next.spotify_uri == TRACK_B
    assert run._next.engine_duration_ms == 3_000


@pytest.mark.parametrize(
    ("align_skip", "event_offset", "expected"),
    [
        (0, 1000, 1000),
        (0, 1001, 1008),
        # an item that starts mid-frame keeps the capture's frame grid
        (3, 1001, 1003),
        (3, 1004, 1011),
    ],
)
def test_a_fallback_cut_ends_on_a_whole_frame(
    align_skip: int, event_offset: int, expected: int
) -> None:
    """Cut mid-frame, the next item would start with its channels swapped."""
    item = _Item(TRACK_A, 0, None, max_lead_trim_s=_MAX_LEAD_TRIM_S)
    item.align_skip = align_skip
    item.raw_read = event_offset
    assert item.end_offset(event_offset) == expected


def test_a_cut_behind_the_read_ends_on_the_next_whole_frame() -> None:
    """A reader already past the exact end cuts where it is, rounded up to a frame."""
    item = _Item(TRACK_A, 0, None, max_lead_trim_s=_MAX_LEAD_TRIM_S)
    item.engine_duration_ms = 2_000
    item.raw_read = 2 * _BYTES_PER_SECOND + 5
    assert item.end_offset(item.raw_read) == 2 * _BYTES_PER_SECOND + _FRAME_BYTES


async def test_a_move_after_a_partial_frame_realigns_the_next_item(tmp_path: Path) -> None:
    """The bytes finishing the frame a drain stopped in are dropped and counted by the cut."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    run._raw_total = 10 * _FRAME_BYTES + 3

    async def _play(uri: str | None = None, **_kwargs: Any) -> None:
        await run._handle_event(
            _playback_event("playing", uri=uri, position_ms=0, duration_ms=2_000)
        )

    _client(run).play.side_effect = _play
    item = await run.move_to(TRACK_C, 0, _streamdetails_for(uri=TRACK_C))
    assert item.align_skip == _FRAME_BYTES - 3

    await run._route(b"\x07" * item.align_skip + _audio(1.2))
    assert item.raw_read == item.align_skip + len(_audio(1.2))
    await run._handle_event(_track_event(TRACK_B))
    assert item.end_at == item.align_skip + 2 * _BYTES_PER_SECOND
    assert _cushioned(item) == _audio(1.2)


@pytest.mark.parametrize("ending", ["stop", "fail"])
async def test_a_cleanly_ended_item_still_drains_when_the_run_ends(
    tmp_path: Path, ending: str
) -> None:
    """An item ended ahead of the capture is owed its cushioned tail, then its stream ends."""
    run = _live_run(tmp_path)
    item = run.current_item
    await _capture(run, _audio(2))
    item.end_at = item.raw_read + _BYTES_PER_SECOND
    if ending == "stop":
        await run.stop()
    else:
        run._fail("the engine broke")
    assert item.delivery_done
    assert item.error is None
    async with asyncio.timeout(5):
        assert await _collect(run, item) == _audio(2)


@pytest.mark.parametrize("full", [False, True])
async def test_claiming_the_capture_target_resumes_a_held_sink(tmp_path: Path, full: bool) -> None:
    """A capture held for a request runs on once that request comes, unless its cushion is full."""
    run = _live_run(tmp_path)
    _, upcoming = await _cross_fed_boundary(run)
    run._sink_running = False
    while full and not upcoming.chunks.full():
        upcoming.chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    sink = _sink(run)
    resumes = sink.resume.await_count

    claimed = await run.claim(TRACK_B, 0, _streamdetails_for(uri=TRACK_B))
    assert claimed is upcoming
    assert sink.resume.await_count == resumes + (0 if full else 1)


async def test_the_unclaimed_clock_starts_at_the_cut_when_the_item_before_let_go(
    tmp_path: Path,
) -> None:
    """With the stream before it already done, the next request is due as soon as the cut lands."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing = run.current_item
    await _feed(run)
    await _capture(run, _audio(1.5))
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))
    upcoming = run._next
    assert upcoming is not None

    await backend.release_item(run, outgoing)
    assert upcoming.unclaimed_since is None
    await _capture(run, _audio(0.5, 2))
    assert run.current_item is upcoming
    assert _unclaimed_since(upcoming) is not None


async def test_a_quiet_discard_keeps_a_run_a_stream_took_up(tmp_path: Path) -> None:
    """A stand-down only stops a run that still serves nobody."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    run.stop = AsyncMock()  # type: ignore[method-assign]

    await backend.discard_run(run, unless_busy=True)
    run.stop.assert_not_called()
    assert backend._run is run

    run.current_item.streaming = False
    await backend.discard_run(run, unless_busy=True)
    run.stop.assert_awaited_once()
    assert backend._run is None


async def test_the_idle_stop_spares_a_run_moved_in_the_meantime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request taking the run up while its idle stop waits for the lock keeps it running."""
    monkeypatch.setattr(soloist_backend, "_IDLE_GRACE_S", 0.01)
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    _confirm_seeks(run)
    backend._run = run
    tasks = _run_tasks(run)

    await backend.release_item(run, run.current_item)
    async with backend._run_lock:
        async with asyncio.timeout(5):
            while not tasks:
                await asyncio.sleep(0.01)
        # what _acquire_item does for a seek, holding the lock the stop waits on
        await run.move_to(TRACK_A, 30_000, _streamdetails_for(uri=TRACK_A))
    await asyncio.gather(*tasks)
    assert not run.stopped
    assert backend._run is run


async def test_feeding_waits_out_the_accounts_own_queue(tmp_path: Path) -> None:
    """Items the account queued itself would play before a fed one."""
    run = _live_run(tmp_path)
    await run._handle_event(_queue_event(("own", TRACK_C)))
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_B)
    _client(run).add_to_queue.assert_not_called()
    assert run._fed is None


async def test_a_move_for_another_queue_adopts_it(tmp_path: Path) -> None:
    """The run serves the queue that asked last; its normalization agreed with the engine's."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    run.engine_normalizes = run._engine_normalization_enabled(run.queue_id)
    _confirm_seeks(run)
    await run.move_to(TRACK_A, 30_000, _streamdetails_for(uri=TRACK_A, queue_id="player2"))
    assert run.queue_id == "player2"


async def test_a_move_for_a_queue_normalizing_otherwise_is_refused(tmp_path: Path) -> None:
    """The engine reads its normalization at startup only, so a fresh run must serve that queue."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    run.engine_normalizes = not run._engine_normalization_enabled(run.queue_id)
    with pytest.raises(AudioError, match="normalizes for another player"):
        await run.move_to(TRACK_A, 30_000, _streamdetails_for(uri=TRACK_A, queue_id="player2"))
    _client(run).seek.assert_not_called()


async def test_a_reset_connection_while_feeding_queues_nothing(tmp_path: Path) -> None:
    """A dropped WebSocket is no reason to fail the item being streamed."""
    run = _live_run(tmp_path)
    _client(run).add_to_queue.side_effect = ConnectionResetError
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_B)
    assert run._fed is None


@pytest.mark.parametrize("command", ["play", "seek"])
async def test_a_reset_connection_during_a_move_fails_it_unbound(
    tmp_path: Path, command: str
) -> None:
    """A move that dropped the connection leaves no item held by a stream, so a fresh run can serve it."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    run.current_item.streaming = False
    getattr(_client(run), command).side_effect = ConnectionResetError
    uri = TRACK_C if command == "play" else TRACK_A
    with pytest.raises(AudioError, match="did not move"):
        await run.move_to(uri, 30_000, _streamdetails_for(uri=uri))
    assert not run.busy
    if command == "seek":
        # the item was in place already: it is left to nobody
        assert run.current_item.seek_target_ms == 30_000
        assert run.current_item.media_key is None


async def test_an_engine_exit_finishes_the_claimed_next_item_too(tmp_path: Path) -> None:
    """Neither the outgoing item nor the one claimed after it is left waiting on a dead engine."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    outgoing = run.current_item
    await _feed(run)
    await run._handle_event(
        _playback_event("playing", uri=TRACK_A, position_ms=0, duration_ms=2_000)
    )
    await _capture(run, _audio(1.5))
    await run._handle_event(_queue_event())
    await run._handle_event(_track_event(TRACK_B))
    details_b = _streamdetails_for(uri=TRACK_B)
    details_b.duration = 180
    _, upcoming = await backend._acquire_item(TRACK_B, 0, details_b, continuation=False)
    assert upcoming is run._next

    run._reader = asyncio.StreamReader()
    run._engine_exited = True
    run._proc = MagicMock(returncode=1)
    async with asyncio.timeout(5):
        await run._read_capture()
    assert outgoing.delivery_done
    assert upcoming.delivery_done
    async with asyncio.timeout(5):
        assert await _collect(run, outgoing) == _audio(1.5)
        with pytest.raises(AudioError, match="engine exit code 1"):
            await _collect(run, upcoming)


@pytest.mark.parametrize(
    ("switched", "budget_s"), [(False, _MAX_LEAD_TRIM_S), (True, _MAX_SWITCH_TRIM_S)]
)
def test_only_a_switched_item_gets_the_switch_trim(
    tmp_path: Path, switched: bool, budget_s: float
) -> None:
    """A seek in place renders no switch silence, so it keeps the small lead budget."""
    run = _make_run(tmp_path)
    item = run._new_item(TRACK_A, 30_000, switched=switched)
    assert item.shaper._max_lead_trim == int(budget_s * _BYTES_PER_SECOND)


async def test_engine_exit_padding_ends_the_streams_at_once(tmp_path: Path) -> None:
    """Zeros the sink keeps rendering after the engine died never keep a consumer waiting."""
    run = _live_run(tmp_path)
    run._reader = reader = asyncio.StreamReader()
    run._engine_exited = True
    run._proc = MagicMock(returncode=1)
    reader.feed_data(b"\x00" * 4 * _CHUNK)
    async with asyncio.timeout(5):
        await run._read_capture()
    assert run.current_item.delivery_done
    _sink(run).suspend.assert_awaited()
    # it stopped on the first padding read, not once the pipe ran dry
    assert await asyncio.wait_for(reader.read(1), 0.1)


def test_only_the_switch_silence_counts_towards_a_switched_items_end() -> None:
    """A silent intro trimmed along with the switch silence is part of the track's length."""
    item = _Item(TRACK_B, 0, None, max_lead_trim_s=1.5, max_counted_lead_s=0.6)
    item.engine_duration_ms = 2_000
    # half a second of switch silence plus a second of silent intro
    item.shaper._lead_skipped = int(1.5 * _BYTES_PER_SECOND) // _FRAME_BYTES * _FRAME_BYTES
    expected = round(0.6 * 44_100) * _FRAME_BYTES + round(2.0 * 44_100) * _FRAME_BYTES
    assert item.end_offset(expected) == expected


@pytest.mark.parametrize("same_setting", [True, False])
async def test_a_claim_for_another_queue_needs_its_normalization(
    tmp_path: Path, same_setting: bool
) -> None:
    """Audio the engine normalizes for one player is not handed to a player that does not."""
    run = _live_run(tmp_path)
    run.queue_id = "player1"
    run.engine_normalizes = True
    run._engine_normalization_enabled = MagicMock(  # type: ignore[method-assign]
        return_value=same_setting
    )
    await _cross_fed_boundary(run)
    details = _streamdetails_for(uri=TRACK_B, queue_id="player2")
    claimed = await run.claim(TRACK_B, 0, details)
    if same_setting:
        assert claimed is run.current_item
        assert run.queue_id == "player2"
    else:
        assert claimed is None
        assert run.current_item.media_key is None
        assert run.queue_id == "player1"


@pytest.mark.parametrize("step", ["claim", "move_to"])
async def test_a_cancelled_take_up_lets_the_run_go(tmp_path: Path, step: str) -> None:
    """An acquisition cut short half-way leaves nothing holding the engine."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await backend.release_item(run, run.current_item)
    tasks = _run_tasks(run)
    if step == "claim":
        run.claim = AsyncMock(side_effect=asyncio.CancelledError)  # type: ignore[method-assign]
    else:
        run.claim = AsyncMock(return_value=None)  # type: ignore[method-assign]
        run.move_to = AsyncMock(side_effect=asyncio.CancelledError)  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)
    await asyncio.gather(*tasks, return_exceptions=True)
    assert run.stopped
    assert backend._run is None


async def test_a_cancelled_take_up_spares_the_run_the_next_request_took(
    tmp_path: Path,
) -> None:
    """A request waiting on the cut-short one keeps the run it took up."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await backend.release_item(run, run.current_item)
    tasks = _run_tasks(run)
    entered = asyncio.Event()

    async def move_to(
        spotify_uri: str, seek_position_ms: int, streamdetails: StreamDetails | None
    ) -> _Item:
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        item = _Item(spotify_uri, seek_position_ms, streamdetails, max_lead_trim_s=0)
        run._item = item
        return item

    run.claim = AsyncMock(return_value=None)  # type: ignore[method-assign]
    run.move_to = move_to  # type: ignore[method-assign]
    first = asyncio.ensure_future(
        backend._acquire_item(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)
    )
    await entered.wait()
    second = asyncio.ensure_future(
        backend._acquire_item(TRACK_C, 0, _streamdetails_for(uri=TRACK_C), continuation=False)
    )
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    taken_run, item = await second
    await asyncio.gather(*tasks, return_exceptions=True)
    assert taken_run is run
    assert item.streaming
    assert not run.stopped
    assert backend._run is run


async def test_the_drain_waits_for_a_capture_busy_elsewhere(tmp_path: Path) -> None:
    """Audio read but not yet taken by a capture blocked on a cushion is not yet drained."""
    run = _live_run(tmp_path)
    run._reader = asyncio.StreamReader()
    run._read_waiting = False
    drain = asyncio.ensure_future(run._drain())
    await asyncio.sleep(0.15)
    assert not drain.done()
    run._read_waiting = True
    await asyncio.wait_for(drain, 1)


@pytest.mark.parametrize("paused", [True, False])
async def test_a_move_in_place_wakes_an_engine_paused_past_its_last_item(
    tmp_path: Path, paused: bool
) -> None:
    """Replaying the track the engine stopped on needs it playing again, not only seeked."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=180_000))
    if paused:
        await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    _confirm_seeks(run)

    await run.move_to(TRACK_A, 0, _streamdetails_for(uri=TRACK_A))
    client = _client(run)
    client.seek.assert_awaited_with(0)
    if paused:
        client.resume.assert_awaited_once()
    else:
        client.resume.assert_not_called()


async def test_a_queue_report_in_flight_keeps_the_fed_item(tmp_path: Path) -> None:
    """A report sent before the engine took the add in leaves the item waiting for its listing."""
    run = _live_run(tmp_path)
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_B)
    await run._handle_event(_queue_event())
    fed = run._fed
    assert fed is not None
    assert fed.uid is None
    await run._handle_event(_queue_event(("q1", TRACK_B)))
    assert fed.uid == "q1"


async def test_a_jump_never_skips_to_an_unconfirmed_fed_item(tmp_path: Path) -> None:
    """An item no queue report listed was most likely refused, so the jump plays directly."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_B)
    client = _client(run)
    client.play.side_effect = lambda uri, **_: run._observe_engine_item(uri, announced=True)

    item = await run.move_to(TRACK_C, 0, _streamdetails_for(uri=TRACK_C))
    client.skip_next.assert_not_called()
    client.play.assert_awaited_once_with(TRACK_C)
    assert run._fed is None
    assert item.spotify_uri == TRACK_C


async def test_an_unconfirmed_fed_item_never_reads_as_streaming_elsewhere(
    tmp_path: Path,
) -> None:
    """With a refused item pending, the engine moving on by itself ends the item instead."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await run.feed_next(cast("str", run.current_item.media_key), TRACK_B)
    await run._handle_event(_track_event(TRACK_C))
    assert run._error is None
    assert run.current_item.ended
    assert run._fed is None


async def test_a_rewound_pause_at_the_end_wins_over_a_queued_item(tmp_path: Path) -> None:
    """The engine stopping instead of moving on ends the item, whatever was queued."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=2_000))
    await _feed(run)
    await _capture(run, _audio(1.5))
    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    assert run.current_item.ended
    assert run._fed is None
    assert run._next is None


async def test_an_ended_item_gets_its_tail_from_a_paused_engine(tmp_path: Path) -> None:
    """A full cushion at the end of the last item still lets the tail out of the sink."""
    run = _live_run(tmp_path)
    item = run.current_item
    item.end_at = 10 * _BYTES_PER_SECOND
    run._engine_playing = False
    while not item.chunks.full():
        item.chunks.put_nowait(b"\x01")
    blocked = asyncio.ensure_future(run._hand_over(item, b"\x02"))
    await asyncio.sleep(0.01)
    _sink(run).suspend.assert_awaited()
    item.chunks.get_nowait()
    assert await blocked is True
    _sink(run).resume.assert_awaited()


async def test_a_claim_that_finds_nothing_keeps_the_queue(tmp_path: Path) -> None:
    """Asking from another queue only takes the run over once an item is actually claimed."""
    run = _live_run(tmp_path)
    run.queue_id = "player1"
    run.engine_normalizes = True
    run._engine_normalization_enabled = MagicMock(return_value=True)  # type: ignore[method-assign]
    details = _streamdetails_for(uri=TRACK_C, queue_id="player2")
    assert await run.claim(TRACK_C, 0, details) is None
    assert run.queue_id == "player1"


async def test_room_made_by_a_release_does_not_wake_the_sink(tmp_path: Path) -> None:
    """The release that emptied the cushion holds the engine; the woken write must not undo it."""
    run = _live_run(tmp_path)
    item = run.current_item
    run._engine_playing = True
    while not item.chunks.full():
        item.chunks.put_nowait(b"\x01")
    blocked = asyncio.ensure_future(run._hand_over(item, b"\x02"))
    await asyncio.sleep(0.01)
    sink = _sink(run)
    sink.resume.reset_mock()
    await run.release(item)
    assert await asyncio.wait_for(blocked, 2) is True
    sink.resume.assert_not_called()


async def test_the_engine_is_kept_at_unity_volume(tmp_path: Path) -> None:
    """A volume change from the Spotify app would scale the captured audio."""
    run = _live_run(tmp_path)
    await run._handle_event(
        SoloistEvent(type="volume_changed", data=SoloistVolumeChanged(volume=40), raw={})
    )
    _client(run).set_volume.assert_awaited_once_with(100)
    _client(run).set_volume.reset_mock()
    await run._handle_event(
        SoloistEvent(type="volume_changed", data=SoloistVolumeChanged(volume=100), raw={})
    )
    _client(run).set_volume.assert_not_called()


async def test_a_queued_item_removed_in_the_app_is_no_boundary(tmp_path: Path) -> None:
    """The entry leaving the queue without the engine moving on leaves the item playing."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=2_000))
    await _feed(run)
    await _capture(run, _audio(1.5))
    await run._handle_event(_queue_event())
    await run._handle_event(_playback_event("playing", uri=TRACK_A, position_ms=1_500))
    assert not run.current_item.ended
    assert run._next is None
    # the engine stopping at the item's end still ends it
    await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=0))
    assert run.current_item.end_at is not None
    assert _fed(run) is None


async def test_a_removed_repeat_of_the_current_track_is_no_boundary(tmp_path: Path) -> None:
    """Early in the track, a playing report after its queued repeat was removed is no restart."""
    run = _live_run(tmp_path)
    await run._handle_event(_playback_event("playing", uri=TRACK_A, duration_ms=180_000))
    await _feed(run, TRACK_A)
    await _capture(run, _audio(1.5))
    await run._handle_event(_queue_event())
    await run._handle_event(_playback_event("playing", uri=TRACK_A, position_ms=1_500))
    assert not run.current_item.ended
    assert run._next is None


def _shorten_stall_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the capture give up on a silent FIFO within a fraction of a second."""
    monkeypatch.setattr(soloist_backend, "_READ_SLICE_S", 0.01)
    monkeypatch.setattr(soloist_backend, "_STALL_TIMEOUT_S", 0.05)


@pytest.mark.parametrize("status", ["playing", "buffering"])
async def test_an_engine_that_stops_sending_audio_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A rebuffer that never recovers suspends the sink, and still ends in an error."""
    _shorten_stall_timeout(monkeypatch)
    run = _live_run(tmp_path)
    run._reader = asyncio.StreamReader()
    await run._handle_event(_playback_event(status, uri=TRACK_A))
    async with asyncio.timeout(5):
        await run._read_capture()
    assert run._error == "Spotify stopped sending audio"


@pytest.mark.parametrize("hold", ["paused", "cushion_full", "released", "moving"])
async def test_a_sink_held_on_purpose_is_no_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    """A suspension of our own may outlast the stall timeout: the consumer can stop for minutes."""
    _shorten_stall_timeout(monkeypatch)
    run = _live_run(tmp_path)
    run._reader = asyncio.StreamReader()
    item = run.current_item
    if hold == "paused":
        await run._handle_event(_playback_event("paused", uri=TRACK_A, position_ms=60_000))
    else:
        await run._handle_event(_playback_event("buffering", uri=TRACK_A))
    if hold == "cushion_full":
        while not item.chunks.full():
            item.chunks.put_nowait(b"\x01")
    elif hold == "released":
        item.streaming = False
    elif hold == "moving":
        run._moving = True
    reading = asyncio.ensure_future(run._read_capture())
    await asyncio.sleep(0.2)
    assert run._error is None
    run._stopped = True
    await asyncio.wait_for(reading, 1)
    for task in run._tasks:
        task.cancel()


def _options_event(report: str, *, repeat: str = "off", shuffle: bool = False) -> SoloistEvent:
    """Return an options report, as its own event or as part of a playback state."""
    options = SoloistPlaybackOptions(shuffle=shuffle, repeat=repeat)
    if report == "options_changed":
        return SoloistEvent(type=report, data=SoloistOptionsChanged(options=options), raw={})
    item = SoloistEntity(uri=TRACK_A, entity_type="track")
    return SoloistEvent(
        type=report,
        data=SoloistPlaybackState(status="playing", item=item, options=options),
        raw={},
    )


@pytest.mark.parametrize("report", ["options_changed", "playback_state"])
@pytest.mark.parametrize(
    ("repeat", "shuffle"), [("track", False), ("context", False), ("off", True)]
)
async def test_shuffle_or_repeat_switched_on_in_spotify_is_switched_off(
    tmp_path: Path, report: str, repeat: str, shuffle: bool
) -> None:
    """Repeat would loop an item past its end and shuffle reorder the items it is given."""
    run = _live_run(tmp_path)
    client = _client(run)
    reset = [
        call.set_repeat_track(False, await_result=True),
        call.set_repeat_context(False, await_result=True),
        call.set_shuffle(False, await_result=True),
    ]

    def _resets() -> list[Any]:
        return [entry for entry in client.mock_calls if entry in reset]

    await run._handle_event(_options_event(report))
    # the engine repeats its options across reports: one reset per switch
    for _ in range(2):
        await run._handle_event(_options_event(report, repeat=repeat, shuffle=shuffle))
    await asyncio.gather(*run._tasks)
    assert _resets() == reset
    await run._handle_event(_options_event(report))
    await run._handle_event(_options_event(report, repeat=repeat, shuffle=shuffle))
    await asyncio.gather(*run._tasks)
    assert _resets() == reset * 2


async def test_a_toggled_normalization_switch_gets_the_next_item_a_fresh_run(
    tmp_path: Path,
) -> None:
    """The engine reads its normalization at startup only, even for the queue it plays for."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await _cross_fed_boundary(run)
    # the player's switch was turned on while the run played
    run._engine_normalization_enabled = MagicMock(  # type: ignore[method-assign]
        return_value=not run.engine_normalizes
    )
    run.stop = AsyncMock()  # type: ignore[method-assign]
    fresh = MagicMock()
    backend._start_run = AsyncMock(return_value=fresh)  # type: ignore[method-assign]
    details = _streamdetails_for(uri=TRACK_B)
    assert details.queue_id == run.queue_id
    acquired = await backend._acquire_item(TRACK_B, 0, details, continuation=False)
    assert acquired == (fresh, fresh.current_item)
    assert run.current_item.media_key is None
    run.stop.assert_awaited()
    backend._start_run.assert_awaited_once_with(TRACK_B, 0, details)


async def test_an_item_reached_before_the_engine_died_gets_a_fresh_run(tmp_path: Path) -> None:
    """The queued item the engine moved on to is not failed along with the dead engine."""
    backend = _streamable_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    await _cross_fed_boundary(run)
    run._engine_exited = True
    run._proc = MagicMock(returncode=1)
    run.stop = AsyncMock()  # type: ignore[method-assign]
    fresh = MagicMock()
    backend._start_run = AsyncMock(return_value=fresh)  # type: ignore[method-assign]
    details = _streamdetails_for(uri=TRACK_B)
    acquired = await backend._acquire_item(TRACK_B, 0, details, continuation=False)
    assert acquired == (fresh, fresh.current_item)
    assert run.current_item.media_key is None
    backend._start_run.assert_awaited_once_with(TRACK_B, 0, details)


async def test_a_hold_between_two_rebuffers_starts_the_stall_clock_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip during a long rebuffer leaves the item it moves to the full stall timeout."""
    monkeypatch.setattr(soloist_backend, "_READ_SLICE_S", 0.01)
    monkeypatch.setattr(soloist_backend, "_STALL_TIMEOUT_S", 0.1)
    run = _live_run(tmp_path)
    run._reader = asyncio.StreamReader()
    await run._handle_event(_playback_event("buffering", uri=TRACK_A))
    reading = asyncio.ensure_future(run._read_capture())
    await asyncio.sleep(0.07)
    run._moving = True
    await asyncio.sleep(0.05)
    run._moving = False
    await asyncio.sleep(0.07)
    assert run._error is None
    run._stopped = True
    await asyncio.wait_for(reading, 1)


@pytest.mark.parametrize("error", [TimeoutError(), SoloistError("not connected")])
async def test_a_failed_option_reset_is_tried_again(tmp_path: Path, error: Exception) -> None:
    """A reset lost to a dropped connection is retried on the next report that still shows repeat."""
    run = _live_run(tmp_path)
    client = _client(run)
    client.set_repeat_track.side_effect = [error, None]
    # a closed socket fails the command, which the reset sees like a timeout
    client.connected = not isinstance(error, SoloistError)
    for _ in range(2):
        await run._handle_event(_options_event("options_changed", repeat="track"))
        await asyncio.gather(*run._tasks)
    assert client.set_repeat_track.await_count == 2
    client.set_shuffle.assert_awaited_once_with(False, await_result=True)


async def test_a_move_between_two_read_slices_starts_the_stall_clock_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seek that lands within one read slice still leaves its item the full stall timeout."""
    monkeypatch.setattr(soloist_backend, "_READ_SLICE_S", 0.05)
    monkeypatch.setattr(soloist_backend, "_STALL_TIMEOUT_S", 0.2)
    run = _live_run(tmp_path)
    run._reader = asyncio.StreamReader()
    _confirm_seeks(run)
    await run._handle_event(_playback_event("playing", uri=TRACK_A))
    await run._handle_event(_playback_event("buffering", uri=TRACK_A))
    reading = asyncio.ensure_future(run._read_capture())
    await asyncio.sleep(0.17)
    await run.move_to(TRACK_A, 30_000, _streamdetails_for(uri=TRACK_A))
    await asyncio.sleep(0.12)
    assert run._error is None
    run._stopped = True
    await asyncio.wait_for(reading, 1)


async def test_repeat_tapped_again_during_a_reset_gets_a_second_round(tmp_path: Path) -> None:
    """Repeat-one is two taps in the Spotify app, the second while the first reset still runs."""
    run = _live_run(tmp_path)
    client = _client(run)
    acked = asyncio.Event()

    async def _await_ack(*_args: Any, **_kwargs: Any) -> None:
        await acked.wait()

    client.set_repeat_context.side_effect = _await_ack
    await run._handle_event(_options_event("options_changed", repeat="context"))
    while not client.set_repeat_context.await_count:
        await asyncio.sleep(0)
    await run._handle_event(_options_event("options_changed", repeat="track"))
    acked.set()
    await asyncio.gather(*run._tasks)
    round_of_resets = [
        call.set_repeat_track(False, await_result=True),
        call.set_repeat_context(False, await_result=True),
        call.set_shuffle(False, await_result=True),
    ]
    assert [entry for entry in client.mock_calls if entry in round_of_resets] == (
        round_of_resets * 2
    )
    assert run._options_reset is None


async def test_session_normalizes_leaves_a_toggled_switch_to_the_fresh_run(tmp_path: Path) -> None:
    """A seek after the switch changed gets a fresh run, so the old one must not answer for it."""
    backend = _make_backend(tmp_path)
    run = _live_run(tmp_path, backend=backend)
    backend._run = run
    run.engine_normalizes = False
    # the player's switch was turned on while the run played
    run._engine_normalization_enabled = MagicMock(return_value=True)  # type: ignore[method-assign]
    assert backend.session_normalizes(_streamdetails_for(uri=TRACK_A)) is None


_real_sleep = asyncio.sleep
_CHUNK_S = _READ_CHUNK_SIZE / _BYTES_PER_SECOND


class _VirtualFifo:
    """
    A capture FIFO on a virtual clock that only moves while the capture waits.

    The pipe sink renders on demand, so a read returns a full chunk at once unless
    the engine delivers slower than that; the consumer takes the audio as it comes.
    """

    def __init__(
        self, run: _EngineRun, monkeypatch: pytest.MonkeyPatch, rate: float | None = None
    ) -> None:
        """
        Have the run's capture read this FIFO and pace itself on its clock.

        :param run: The run whose capture reads the FIFO.
        :param monkeypatch: Patches the event loop's clock and the pacing sleep.
        :param rate: How fast the engine delivers, relative to realtime; None renders on demand.
        """
        self.run = run
        self.rate = rate
        self.now = 0.0
        # when the capture got to read again after a stalled loop or a held cushion
        self.resumed_at = 0.0
        self.reads: list[tuple[float, int]] = []
        self.sleeps: list[float] = []
        self._until = 0.0
        self._total = 0
        self._stall: tuple[float, float] | None = None
        self._hold: tuple[float, float] | None = None
        self._release: asyncio.Future[None] | None = None
        run._reader = cast("asyncio.StreamReader", self)
        monkeypatch.setattr(asyncio.get_running_loop(), "time", self.time)
        monkeypatch.setattr(
            "music_assistant.providers.spotify.backends.soloist.asyncio.sleep", self.sleep
        )

    def stall_loop(self, at: float, seconds: float) -> None:
        """Stall the event loop once the given time is reached, with audio waiting in the FIFO."""
        self._stall = (at, seconds)

    def hold_cushion(self, at: float, seconds: float) -> None:
        """Have the consumer stop taking audio for a while once the given time is reached."""
        self._hold = (at, seconds)

    async def read_until(self, until: float) -> None:
        """Run the capture until the virtual clock reaches the given time."""
        self._until = until
        await self.run._read_capture()
        if self._release is not None:
            await self._release

    def read_s(self, start: float, end: float) -> float:
        """Return the seconds of audio the capture read from the given start until its end."""
        read = sum(size for at, size in self.reads if start <= at < end)
        return read / _BYTES_PER_SECOND

    def time(self) -> float:
        """Return the virtual time."""
        return self.now

    async def sleep(self, delay: float) -> None:
        """Move the virtual clock on instead of waiting."""
        self.sleeps.append(delay)
        self.now += delay
        await _real_sleep(0)

    async def read(self, size: int) -> bytes:
        """Return a chunk once the engine delivered it; no read ever waits out a read slice."""
        if self.now >= self._until:
            self.run._stopped = True
        if self.rate is not None:
            self.now = max(self.now, (self._total + size) / (_BYTES_PER_SECOND * self.rate))
        if self._stall is not None and self.now >= self._stall[0]:
            self.now += self._stall[1]
            self.resumed_at = self.now
            self._stall = None
        chunks = self.run.current_item.chunks
        if self._hold is not None and self.now >= self._hold[0]:
            # this chunk finds the cushion full and waits for room
            while not chunks.full():
                chunks.put_nowait(b"\x01")
            self._release = asyncio.ensure_future(self._take_audio_after(self._hold[1]))
            self._hold = None
        else:
            while not chunks.empty():
                chunks.get_nowait()
        self._total += size
        self.reads.append((self.now, size))
        return b"\x01" * size

    async def _take_audio_after(self, seconds: float) -> None:
        """Make room in the cushion again once the capture has waited the given time on it."""
        # first runs when the capture waits on the full cushion: nothing yields before that
        self.now += seconds
        self.resumed_at = self.now
        chunks = self.run.current_item.chunks
        while not chunks.empty():
            chunks.get_nowait()


async def test_the_capture_reads_its_burst_and_then_its_pace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipe sink rendering on demand is read at the pace, apart from the initial burst."""
    run = _live_run(tmp_path)
    fifo = _VirtualFifo(run, monkeypatch)
    await fifo.read_until(10.0)
    assert fifo.read_s(0.0, 10.0) == pytest.approx(
        (_PACE_BURST_S + 10.0) * _PACE_RATE, abs=_CHUNK_S
    )
    assert fifo.read_s(5.0, 10.0) == pytest.approx(5.0 * _PACE_RATE, abs=_CHUNK_S)


async def test_a_capture_behind_its_pace_never_waits_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine delivering below the pace is read as fast as it delivers, however long it plays."""
    run = _live_run(tmp_path)
    fifo = _VirtualFifo(run, monkeypatch, rate=1.08)
    # long enough for the time it falls behind the pace to outgrow the burst
    await fifo.read_until(120.0)
    assert not fifo.sleeps
    assert fifo.read_s(0.0, 120.0) == pytest.approx(120.0 * 1.08, abs=_CHUNK_S)


@pytest.mark.parametrize("gap", ["stalled_loop", "full_cushion"])
async def test_time_the_capture_could_not_read_is_not_made_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gap: str
) -> None:
    """Catching up would be an unpaced burst, which PulseAudio fills with silence."""
    run = _live_run(tmp_path)
    fifo = _VirtualFifo(run, monkeypatch)
    if gap == "stalled_loop":
        fifo.stall_loop(at=2.0, seconds=5.0)
    else:
        fifo.hold_cushion(at=2.0, seconds=5.0)
    await fifo.read_until(10.0)
    if gap == "full_cushion":
        # the capture really waited on the full cushion
        _sink(run).suspend.assert_awaited_once()
    assert fifo.resumed_at == pytest.approx(7.0, abs=_CHUNK_S)
    # the burst and the pace, as from a fresh start
    assert fifo.read_s(fifo.resumed_at, fifo.resumed_at + 1.0) == pytest.approx(
        (_PACE_BURST_S + 1.0) * _PACE_RATE, abs=2 * _CHUNK_S
    )
