"""
Unit tests for the Spotify Soloist playback backend.

The backend spawns one engine run per Spotify URI (single-track mode) and
streams the captured PCM as that item's audio. These tests lock down the pure
logic around that: lead-silence trimming, the run's cushion and its sink
backpressure, tail-padding suppression, startup and event handling, delivery
validation, run acquisition (busy/replaced/superseded), paired-session
adoption and setup. No real process or PulseAudio is involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import AudioError, LoginFailed
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import ProviderStreamLimitError
from music_assistant.providers.spotify.backends import StreamSupersededError
from music_assistant.providers.spotify.backends import soloist as soloist_backend
from music_assistant.providers.spotify.backends.soloist import (
    _BYTES_PER_SECOND,
    _FRAME_BYTES,
    _MAX_LEAD_TRIM_S,
    _TAIL_PAD_GRACE_S,
    _TAIL_PAD_ZONE_S,
    SoloistBackend,
    _SingleTrackRun,
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
)

TRACK_A = "spotify:track:aaa"
TRACK_B = "spotify:track:bbb"
# an audiobook is one item whose chapters are separate Spotify URIs
AUDIOBOOK = "spotify:show:book"
CHAPTER_A = "spotify:episode:ch1"
CHAPTER_B = "spotify:episode:ch2"
CHAPTER_C = "spotify:episode:ch3"


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
    media_key: str | None = None,
) -> _SingleTrackRun:
    """Return a run with its process/sink/client replaced by mocks."""
    streamdetails = _streamdetails_for(uri=uri)
    if duration is not None:
        streamdetails.duration = duration
    run = _SingleTrackRun(_make_backend(tmp_path), uri, seek_ms, streamdetails)
    if media_key is not None:
        run.media_key = media_key
    run._sink = AsyncMock()
    run._client = AsyncMock()
    run._proc = MagicMock(returncode=None)
    run._logged_in = True
    run._sink_running = True
    return run


async def _collect(run: _SingleTrackRun) -> bytes:
    """Return everything the run streams, up to where it ended the item."""
    collected = bytearray()
    async for chunk in run.stream():
        collected.extend(chunk)
    return bytes(collected)


def test_scrub_leaves_mid_track_silence_alone(tmp_path: Path) -> None:
    """A quiet passage outside the tail zone is content (the shaper owns the lead)."""
    run = _make_run(tmp_path)
    run._read_bytes = 10 * _BYTES_PER_SECOND
    assert run._scrub(b"\x00" * 1024) == b"\x00" * 1024
    assert run._scrub(b"\x01" * 64) == b"\x01" * 64


def test_scrub_refuses_padding_in_the_items_tail_zone(tmp_path: Path) -> None:
    """Zeros inside the tail zone are the sink idling out the engine's end."""
    run = _make_run(tmp_path, duration=60)
    second = _BYTES_PER_SECOND
    run._read_bytes = 55 * second
    grace = int(_TAIL_PAD_GRACE_S * second)
    # the first moment of padding is kept, the rest refused
    assert run._scrub(b"\x00" * grace) == b"\x00" * grace
    run._tail_zeros = grace
    assert run._scrub(b"\x00" * second) == b""
    # real audio resets the run: the zeros were a quiet passage after all
    assert run._scrub(b"\x01" * 64) == b"\x01" * 64
    assert run._tail_zeros == 0


def test_scrub_leaves_a_short_items_silence_alone(tmp_path: Path) -> None:
    """An item no longer than the zone has no distinguishable tail."""
    run = _make_run(tmp_path, duration=int(_TAIL_PAD_ZONE_S))
    run._read_bytes = int(_TAIL_PAD_ZONE_S - 1) * _BYTES_PER_SECOND
    chunk = b"\x00" * (2 * int(_TAIL_PAD_GRACE_S * _BYTES_PER_SECOND))
    assert run._scrub(chunk) == chunk


async def test_a_full_cushion_pauses_the_engine(tmp_path: Path) -> None:
    """When the consumer stops taking audio, the sink is suspended, not overflowed."""
    run = _make_run(tmp_path)
    sink = cast("AsyncMock", run._sink)
    while not run._chunks.full():
        run._chunks.put_nowait(b"\x01")

    blocked = asyncio.ensure_future(run._hand_over(b"\x02"))
    await asyncio.sleep(0.01)
    assert not blocked.done()
    sink.suspend.assert_awaited_once()

    # the consumer takes a chunk: the write lands and the engine resumes
    run._engine_playing = True
    assert run._chunks.get_nowait() == b"\x01"
    assert await blocked is True
    sink.resume.assert_awaited()


async def test_a_full_cushion_still_ends_the_stream(tmp_path: Path) -> None:
    """The end of delivery survives a cushion with no room left for the sentinel."""
    # no duration: this covers the cushion, not how much audio arrived
    run = _make_run(tmp_path, duration=None)
    while not run._chunks.full():
        run._chunks.put_nowait(b"\x01" * 64)
    run._finish_delivery()
    # nothing may re-signal the end once the consumer drains: the flag carries it
    assert len(await _collect(run)) == run._chunks.maxsize * 64


async def test_a_failed_run_surfaces_its_error_to_the_stream(tmp_path: Path) -> None:
    """The consumer sees the run's real failure, not a clean end."""
    run = _make_run(tmp_path)
    run._chunks.put_nowait(b"\x01" * 64)
    run._fail("the engine broke")
    with pytest.raises(AudioError, match="the engine broke"):
        await _collect(run)


async def test_short_delivery_is_rejected_as_incomplete(tmp_path: Path) -> None:
    """A run still going that stops delivering must not read as a completed stream."""
    run = _make_run(tmp_path, duration=152)
    run._chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._finish_delivery()
    with pytest.raises(AudioError, match="incomplete"):
        await _collect(run)


async def test_a_refused_item_is_reported_as_a_refusal(tmp_path: Path) -> None:
    """An engine that plays nothing and ends the run cleanly was refused the item."""
    run = _make_run(tmp_path, duration=152)
    run._engine_exited = True
    run._proc = MagicMock(returncode=0)
    run._chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._finish_delivery()
    # the message travels on the error itself; the queue reports it where it skips
    with pytest.raises(AudioError, match=f"would not play {TRACK_A}"):
        await _collect(run)


async def test_a_crashed_run_is_not_reported_as_a_refusal(tmp_path: Path) -> None:
    """An engine that died on its own item is a fault, whatever it managed to play."""
    run = _make_run(tmp_path, duration=152)
    run._engine_exited = True
    run._proc = MagicMock(returncode=1)
    run._chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._finish_delivery()
    # names the code, so a refusal that turns out to exit non-zero is recognisable
    with pytest.raises(AudioError, match="engine exit code 1"):
        await _collect(run)


async def test_a_starved_run_is_still_reported_as_incomplete(tmp_path: Path) -> None:
    """A run that played most of its item and then stopped is a fault, not a refusal."""
    run = _make_run(tmp_path, duration=152)
    run._engine_exited = True
    run._proc = MagicMock(returncode=0)
    run._chunks.put_nowait(b"\x01" * (100 * _BYTES_PER_SECOND))
    run._finish_delivery()
    with pytest.raises(AudioError, match="incomplete"):
        await _collect(run)


async def test_a_short_item_is_still_judged_on_what_arrived(tmp_path: Path) -> None:
    """An item shorter than the tolerance would otherwise pass however little arrived."""
    run = _make_run(tmp_path, duration=8)
    run._engine_exited = True
    run._proc = MagicMock(returncode=0)
    run._chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._finish_delivery()
    with pytest.raises(AudioError, match="would not play"):
        await _collect(run)


async def test_a_short_item_played_in_full_is_accepted(tmp_path: Path) -> None:
    """The proportional tolerance must not turn a complete short item into a refusal."""
    run = _make_run(tmp_path, duration=8)
    run._engine_exited = True
    run._proc = MagicMock(returncode=0)
    run._chunks.put_nowait(b"\x01" * (8 * _BYTES_PER_SECOND))
    run._finish_delivery()
    await _collect(run)


async def test_a_seek_to_the_items_end_is_not_read_as_a_refusal(tmp_path: Path) -> None:
    """Audio the seek skipped counts, so a near-complete delivery ends cleanly."""
    run = _make_run(tmp_path, duration=152, seek_ms=151_000)
    run._engine_exited = True
    run._proc = MagicMock(returncode=0)
    run._chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._finish_delivery()
    await _collect(run)


async def test_a_stopped_run_is_not_judged_incomplete(tmp_path: Path) -> None:
    """A consumer that left early is the normal end of an aborted stream."""
    run = _make_run(tmp_path, duration=152)
    run._chunks.put_nowait(b"\x01" * _FRAME_BYTES)
    run._stopped = True
    run._finish_delivery()
    assert await _collect(run) == b"\x01" * _FRAME_BYTES


async def test_a_seek_counts_towards_the_delivery(tmp_path: Path) -> None:
    """Audio skipped by the seek is not audio the engine failed to deliver."""
    run = _make_run(tmp_path, duration=60, seek_ms=55_000)
    run._chunks.put_nowait(b"\x01" * (6 * _BYTES_PER_SECOND))
    run._finish_delivery()
    await _collect(run)


def test_the_own_item_report_starts_the_run_and_refines_the_duration(tmp_path: Path) -> None:
    """The engine reaching the item is what playback start means."""
    run = _make_run(tmp_path, duration=180)
    run._observe_item(TRACK_A, 179_000)
    assert run._started.is_set()
    assert run._duration_ms == 179_000


def test_a_seek_is_confirmed_only_near_its_target(tmp_path: Path) -> None:
    """A pre-seek position report cannot confirm the seek."""
    run = _make_run(tmp_path, seek_ms=60_000)
    run._observe_position(0)
    assert not run._seek_confirmed.is_set()
    run._observe_position(58_000)
    assert run._seek_confirmed.is_set()


def test_single_track_args_carry_the_uri(tmp_path: Path) -> None:
    """The engine is spawned on exactly one URI, in single-track mode."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20})
    backend._binary = tmp_path / "soloist-bin"
    args = backend._session_args(TRACK_A)
    assert "--single-track" in args
    assert args[args.index("--single-track") + 1] == TRACK_A
    # the binary refuses to start without a device name, even though
    # single-track mode never advertises one
    assert "--device-name" in args


async def test_a_run_for_another_item_reports_capacity(tmp_path: Path) -> None:
    """A live run is one stream slot: anything else waits or resolves elsewhere."""
    backend = _make_backend(tmp_path)
    backend._run = _make_run(tmp_path, uri=TRACK_A, media_key=TRACK_A)
    with pytest.raises(ProviderStreamLimitError):
        await backend._acquire_run(TRACK_B, 0, _streamdetails_for(uri=TRACK_B), continuation=False)


async def test_a_replaced_streams_continuation_is_superseded(tmp_path: Path) -> None:
    """A continuation must not take the run back from the stream that replaced it."""
    backend = _make_backend(tmp_path)
    streamdetails = _streamdetails_for(uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK)
    run = _make_run(tmp_path, uri=CHAPTER_B, media_key=streamdetails.uri)
    backend._run = run
    with pytest.raises(StreamSupersededError):
        await backend._acquire_run(CHAPTER_A, 0, streamdetails, continuation=True)


def test_session_normalizes_answers_only_for_the_items_own_run(tmp_path: Path) -> None:
    """Another item's run says nothing about this one."""
    backend = _make_backend(tmp_path)
    run = _make_run(tmp_path, uri=TRACK_A, media_key=_streamdetails_for(uri=TRACK_A).uri)
    run.engine_normalizes = True
    backend._run = run
    assert backend.session_normalizes(_streamdetails_for(uri=TRACK_A)) is True
    assert backend.session_normalizes(_streamdetails_for(uri=TRACK_B)) is None


async def test_the_engine_wandering_on_after_delivery_ends_the_run_cleanly(
    tmp_path: Path,
) -> None:
    """Autoplay reaching the next track right before exit is this item's natural end."""
    run = _make_run(tmp_path, duration=60)
    run._observe_item(TRACK_A, 60_000)
    run.mass.create_task = MagicMock()  # type: ignore[method-assign]
    run._chunks.put_nowait(b"\x01" * (60 * _BYTES_PER_SECOND))
    run._observe_item(TRACK_B, 100_000)
    assert run._error is None
    assert run._item_over is True
    assert await _collect(run) == b"\x01" * (60 * _BYTES_PER_SECOND)
    run.mass.create_task.assert_called_once()


def test_the_engine_starting_on_the_wrong_item_fails_the_run(tmp_path: Path) -> None:
    """Before this run's item ever played, a foreign report is not an ending."""
    run = _make_run(tmp_path)
    run.mass.create_task = MagicMock()  # type: ignore[method-assign]
    run._observe_item(TRACK_B, 100_000)
    assert run._error is not None


async def test_a_seek_replaces_the_held_run(tmp_path: Path) -> None:
    """A positive seek restarts the item's run; only a prefetch must never steal it."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20})
    backend._server = MagicMock()
    backend._binary = tmp_path / "soloist-bin"
    held = _make_run(tmp_path, uri=TRACK_A, media_key=_streamdetails_for(uri=TRACK_A).uri)
    held.stop = AsyncMock()  # type: ignore[method-assign]
    backend._run = held

    with (
        patch(
            "music_assistant.providers.spotify.backends.soloist.SoloistBinaryManager.ensure_fresh",
            AsyncMock(),
        ),
        patch.object(soloist_backend._SingleTrackRun, "start", AsyncMock()),
    ):
        run = await backend._acquire_run(
            TRACK_A, 30, _streamdetails_for(uri=TRACK_A), continuation=False
        )
    held.stop.assert_awaited_once()
    assert run is not held
    assert backend._run is run
