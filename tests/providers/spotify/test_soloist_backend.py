"""
Unit tests for the Spotify Soloist playback backend.

The backend runs one continuous soloist session, feeds it one track ahead and
splits the captured PCM into per-item streams. These tests lock down the pure
logic around that: lead-silence trimming, the item channels and where the
session cuts between them, event handling, the crossfade handed to the engine,
feeding the follower, completeness validation, paired-session adoption and
setup. No real process or PulseAudio is involved.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import AudioError, LoginFailed

from music_assistant.helpers.pulse_capture import CAPTURE_SAMPLE_RATE
from music_assistant.models.music_provider import ProviderStreamLimitError
from music_assistant.providers.spotify.backends import soloist as soloist_backend
from music_assistant.providers.spotify.backends.soloist import (
    _BYTES_PER_SECOND,
    _FRAME_BYTES,
    _IDLE_TIMEOUT_S,
    _MAX_LEAD_TRIM_S,
    SoloistBackend,
    _ItemAudio,
    _SoloistSession,
    _trim_lead_silence,
)
from music_assistant.providers.spotify.constants import (
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    SOLOIST_DATA_DIR_NAME,
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
    SoloistTrackChanged,
    SoloistVolumeChanged,
)

TRACK_A = "spotify:track:aaa"
TRACK_B = "spotify:track:bbb"


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


def test_seek_is_confirmed_only_within_tolerance(tmp_path: Path) -> None:
    """A position report confirms a seek only once it reaches the tolerance window."""
    item = _make_item(tmp_path, TRACK_A)
    item.seek_target_ms = 60_000
    item.observe_position(50_000)
    assert not item.seek_confirmed.is_set()
    item.observe_position(58_500)
    assert item.seek_confirmed.is_set()


def test_small_seek_target_is_not_confirmed_by_a_pre_seek_zero_report(tmp_path: Path) -> None:
    """A position-0 report before the seek lands cannot confirm a small target."""
    item = _make_item(tmp_path, TRACK_A)
    item.seek_target_ms = 1_500
    item.observe_position(0)
    assert not item.seek_confirmed.is_set()
    item.observe_position(1_500)
    assert item.seek_confirmed.is_set()


def test_position_never_regresses_and_stops_at_the_cut(tmp_path: Path) -> None:
    """The furthest position is kept, and reports after the cut belong to the next item."""
    item = _make_item(tmp_path, TRACK_A)
    item.observe_position(120_000)
    # the engine's stop/idle snapshot at the end of an item reports position 0
    item.observe_position(0)
    assert item.last_position_ms == 120_000
    item.close()
    item.observe_position(5_000)
    assert item.last_position_ms == 120_000


async def test_item_stream_ends_where_the_session_moves_on(tmp_path: Path) -> None:
    """An item's audio ends at the track change, and the next item's begins there."""
    session = _make_session(tmp_path)
    item_a = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    session._current = item_a
    item_a.started.set()
    item_a.claim()
    item_a.write(b"a" * 16)
    await session._observe_current(TRACK_B, 200_000)
    item_a.write(b"late" * 4)  # written after the cut: goes nowhere
    chunks = [chunk async for chunk in item_a.read()]
    assert b"".join(chunks) == b"a" * 16
    # the next item exists, carries the duration and now receives the audio
    item_b = session._items[TRACK_B]
    assert session.current is item_b
    assert item_b.duration_ms == 200_000


async def test_the_engines_restored_state_does_not_cut_a_pending_item(
    tmp_path: Path,
) -> None:
    """A daemon reports the item it restored before playing ours; that is not a boundary."""
    session = _make_session(tmp_path)
    requested = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    session._current = requested
    requested.claim()
    # the engine announces the state it came up with, which is someone else's item
    await session._observe_current("spotify:track:restored", 152_000)
    # closing our item here would end its stream before it delivered anything
    assert requested._closed is False
    assert requested.started.is_set() is False
    # ... and the restored item is never offered as an item's audio
    assert session.item_for("spotify:track:restored") is None
    # then ours starts for real, and picks up from there
    await session._observe_current(TRACK_A, 200_000)
    assert session.current is requested
    assert requested.started.is_set() is True
    requested.write(b"\x01" * 32)
    requested.close()
    assert b"".join([chunk async for chunk in requested.read()]) == b"\x01" * 32


async def test_audio_read_before_the_stream_opens_is_kept(tmp_path: Path) -> None:
    """Audio captured before an item's stream opens is buffered, not dropped."""
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    session._current = item
    item.write(b"head" * 8)
    item.claim()
    item.close()
    chunks = [chunk async for chunk in item.read()]
    assert b"".join(chunks) == b"head" * 8


async def test_a_channel_is_only_ever_served_once(tmp_path: Path) -> None:
    """A consumed channel cannot be replayed, so the item needs a fresh session."""
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.started.set()
    assert session.item_for(TRACK_A) is item
    item.claim()
    item.close()
    item.release()
    # this is what a queue holding the same track twice, or repeat wrapping back
    # to the top, asks for: it must not be handed a drained channel
    assert session.item_for(TRACK_A) is None


async def test_an_abandoned_channel_cannot_be_continued(tmp_path: Path) -> None:
    """A stream abandoned mid-item cannot resume where it left off either."""
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.started.set()
    item.claim()
    item.release()
    assert session.item_for(TRACK_A) is None


async def test_a_stuck_item_fails_instead_of_streaming_forever(tmp_path: Path) -> None:
    """An item that runs far past its duration without a track change fails."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.duration_ms = 1_000
    item.claim()
    limit = item._overrun_limit()
    assert limit is not None
    item.write(b"\x01" * (limit + _FRAME_BYTES))
    with pytest.raises(AudioError, match="never moved on"):
        async for _ in item.read():
            pass


async def test_the_first_logged_out_snapshot_is_not_a_lost_pairing(tmp_path: Path) -> None:
    """A daemon reports logged_in=False until it has restored its session."""
    session = _make_session(tmp_path)
    session._logged_in = None
    await session._handle_event(_auth_event(logged_in=False))
    # failing here would break every playback on a perfectly good pairing
    assert session.usable is True
    await session._handle_event(_auth_event(logged_in=True))
    assert session.usable is True


async def test_losing_an_established_login_fails_the_session(tmp_path: Path) -> None:
    """A login that goes away mid-session is real, and ends the session."""
    session = _make_session(tmp_path)
    await session._handle_event(_auth_event(logged_in=True))
    await session._handle_event(_auth_event(logged_in=False))
    assert session.usable is False
    assert session._error == "the session was logged out"


async def test_buffering_gates_the_sink_once_demand_started(tmp_path: Path) -> None:
    """Once PCM demand started, playing runs the sink and buffering suspends it again."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    session._pending.append(TRACK_B)
    sink = _sink_of(session)
    # the sink is created suspended, so there is nothing to suspend yet
    await session._handle_event(_playback_event("buffering"))
    sink.suspend.assert_not_awaited()
    await session._handle_event(_playback_event("playing"))
    sink.resume.assert_awaited_once()
    assert session._current is not None
    assert session._current.playing_seen is True
    # the engine stalling on a rebuffer keeps that silence out of the PCM
    await session._handle_event(_playback_event("buffering"))
    sink.suspend.assert_awaited_once()


async def test_sink_is_not_gated_before_demand_started(tmp_path: Path) -> None:
    """Buffering/playing before PCM demand leave the (still suspended) sink alone."""
    session = _make_session(tmp_path)
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    sink = _sink_of(session)
    await session._handle_event(_playback_event("buffering"))
    await session._handle_event(_playback_event("playing"))
    sink.suspend.assert_not_awaited()
    sink.resume.assert_not_awaited()
    # the status is recorded either way, so session start can decide when to resume
    assert session._current.status == "playing"


@pytest.mark.parametrize("end_status", ["stopped", "idle", "paused"])
async def test_the_last_item_is_drained_rather_than_cut(tmp_path: Path, end_status: str) -> None:
    """However the engine reports the end of a run, the last item drains and closes."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._sink_running = True
    item = session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    item.duration_ms = 1_000
    item.last_position_ms = 1_000
    one_second = 1_000 * CAPTURE_SAMPLE_RATE // 1000 * _FRAME_BYTES
    sink = _sink_of(session)
    await session._handle_event(_playback_event(end_status, position_ms=1_000))
    # the sink stays open for now, so audio still in the FIFO can arrive...
    sink.suspend.assert_not_awaited()
    assert item.draining is True
    assert item._closed is False
    # ... but only that item's own audio is taken, never the padding silence the
    # sink keeps rendering afterwards
    item.write(b"\x01" * one_second)
    item.write(b"\x00" * 4096)
    assert item.buffered == one_second
    await _wait_for(lambda: item._closed)
    sink.suspend.assert_awaited_once()


async def test_an_app_pause_midway_through_the_last_item_is_not_the_end(
    tmp_path: Path,
) -> None:
    """Pausing in the Spotify app halfway through the last track must not truncate it."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._sink_running = True
    item = session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    item.duration_ms = 200_000
    item.last_position_ms = 90_000
    await session._handle_event(_playback_event("paused", position_ms=90_000))
    assert item.draining is False
    assert item._closed is False
    # treated as interference instead: the sink is gated and playback resumed
    _sink_of(session).suspend.assert_awaited_once()
    _client_of(session).resume.assert_awaited_once()


async def test_a_resumed_item_cancels_its_tail_drain(tmp_path: Path) -> None:
    """An armed drain is undone when the engine turns out to have been rebuffering."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._sink_running = True
    item = session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    item.duration_ms = 200_000
    item.last_position_ms = 199_000
    await session._handle_event(_playback_event("stopped", position_ms=199_000))
    armed = item.draining
    await session._handle_event(_playback_event("playing", position_ms=199_500))
    assert armed is True
    assert item.draining is False
    assert item._closed is False
    assert item.drain_task is None


async def test_the_cushion_is_capped_by_suspending_the_sink(tmp_path: Path) -> None:
    """Undelivered audio is handed back as backpressure rather than piling up."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._sink_running = True
    session._engine_playing = True
    item = session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    item.claim()
    sink = _sink_of(session)
    await session._apply_sink_state()
    sink.suspend.assert_not_awaited()
    # the engine has run this far ahead of what the player has taken
    item.write(b"\x01" * int((soloist_backend._MAX_RETAINED_S + 1) * _BYTES_PER_SECOND))
    await session._apply_sink_state()
    sink.suspend.assert_awaited_once()
    assert session._backpressured is True
    # and it comes back once the player has drained enough of it
    item._buffered = int(soloist_backend._RESUME_RETAINED_S * _BYTES_PER_SECOND) - 1
    await session._apply_sink_state()
    sink.resume.assert_awaited_once()
    assert session._backpressured is False


async def test_a_pause_with_more_queued_suspends_the_sink(tmp_path: Path) -> None:
    """A pause while another item is queued behind is ordinary interference, not the end."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._sink_running = True
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    session._pending.append(TRACK_B)
    await session._handle_event(_playback_event("paused"))
    _sink_of(session).suspend.assert_awaited_once()


async def test_nothing_is_sent_before_the_websocket_is_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commands travel over the events socket: a published endpoint is not enough."""
    monkeypatch.setattr(soloist_backend, "_STARTUP_TIMEOUT_S", 0.05)
    session = _make_session(tmp_path)
    client = _client_of(session)
    # the endpoint file exists, but the events task has not connected yet
    client.connected = False
    endpoint_published = asyncio.Event()
    endpoint_published.set()
    with pytest.raises(AudioError, match="did not connect and log in"):
        await session._play(TRACK_A, 0, endpoint_published)
    client.activate.assert_not_awaited()
    client.play.assert_not_awaited()


async def test_nothing_is_sent_before_the_engine_has_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine drops commands sent before it has restored its session."""
    monkeypatch.setattr(soloist_backend, "_STARTUP_TIMEOUT_S", 0.05)
    session = _make_session(tmp_path)
    client = _client_of(session)
    client.connected = True
    # connected, but the engine has not announced its login yet
    session._logged_in = None
    endpoint_published = asyncio.Event()
    endpoint_published.set()
    with pytest.raises(AudioError, match="did not connect and log in"):
        await session._play(TRACK_A, 0, endpoint_published)
    client.activate.assert_not_awaited()
    client.play.assert_not_awaited()


async def test_startup_activates_before_it_plays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh daemon has to become the active device before it is told to play."""
    session = _make_session(tmp_path)
    client = _client_of(session)
    client.connected = True
    monkeypatch.setattr(session, "_await_item_ready", AsyncMock())
    endpoint_published = asyncio.Event()
    endpoint_published.set()
    item = await session._play(TRACK_A, 0, endpoint_published)
    assert item.uri == TRACK_A
    client.activate.assert_awaited_once_with(await_result=True)
    client.play.assert_awaited_once_with(TRACK_A)


async def test_a_refused_start_command_reports_soloist(tmp_path: Path) -> None:
    """A dropped start command surfaces as a Soloist error, not a raw client one."""
    session = _make_session(tmp_path)
    client = _client_of(session)
    client.connected = True
    client.activate.side_effect = SoloistError("websocket is not connected")
    endpoint_published = asyncio.Event()
    endpoint_published.set()
    with pytest.raises(AudioError, match="Spotify Soloist would not start"):
        await session._play(TRACK_A, 0, endpoint_published)


async def test_the_engine_is_told_not_to_shuffle_or_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MA owns the order, and a repeating engine would never reach the item fed behind."""
    session = _make_session(tmp_path)
    client = _client_of(session)
    client.connected = True
    monkeypatch.setattr(session, "_await_item_ready", AsyncMock())
    endpoint_published = asyncio.Event()
    endpoint_published.set()
    await session._play(TRACK_A, 0, endpoint_published)
    client.set_shuffle.assert_awaited_once_with(False)
    client.set_repeat_context.assert_awaited_once_with(False)
    client.set_repeat_track.assert_awaited_once_with(False)


async def test_repeat_turned_on_from_the_app_is_pinned_back_off(tmp_path: Path) -> None:
    """Repeat enabled in the Spotify app is undone before it can loop the item."""
    session = _make_session(tmp_path)
    await session._handle_event(
        SoloistEvent(
            type="options_changed",
            data=SoloistOptionsChanged(
                options=SoloistPlaybackOptions(shuffle=True, repeat="track")
            ),
            raw={},
        )
    )
    client = _client_of(session)
    client.set_shuffle.assert_awaited_once_with(False)
    client.set_repeat_track.assert_awaited_once_with(False)
    client.set_repeat_context.assert_awaited_once_with(False)
    # options that are already off are left alone
    client.set_shuffle.reset_mock()
    await session._handle_event(
        SoloistEvent(
            type="options_changed",
            data=SoloistOptionsChanged(options=SoloistPlaybackOptions()),
            raw={},
        )
    )
    client.set_shuffle.assert_not_awaited()


async def test_a_busy_data_directory_is_reported_as_such(tmp_path: Path) -> None:
    """A daemon left over from an earlier run is named, not reported as a generic failure."""
    session = _make_session(tmp_path)
    # the daemon's own parting complaint, which is all it gives (it exits with 1)
    session._data_dir_busy = True
    with pytest.raises(AudioError, match="Another Spotify Soloist session is still running"):
        session._raise_startup_error("exited before playback started", TRACK_A)


async def test_the_busy_marker_is_picked_up_from_the_daemon_output(tmp_path: Path) -> None:
    """The marker is read off the daemon's stdout, with the API key still redacted."""
    session = _make_session(tmp_path)
    proc = MagicMock()
    lines = [
        'Error: another session is running for data directory "/data/x/soloist-data".',
        "Stop the running session before starting soloist again.",
    ]

    async def _iter_stdout() -> AsyncGenerator[str]:
        for line in lines:
            yield line

    proc.iter_stdout = _iter_stdout
    await session._log_output(proc)
    assert session._data_dir_busy is True


async def test_a_pairing_that_never_logs_in_routes_through_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that cannot log in sends the user back to setup, not a per-track error."""
    session = _make_session(tmp_path)
    unload_with_error = MagicMock()
    monkeypatch.setattr(session.backend.provider, "unload_with_error", unload_with_error)
    await session._handle_event(_auth_event(logged_in=False))
    with pytest.raises(LoginFailed) as err:
        session._raise_startup_error("timed out waiting for playback to start", TRACK_A)
    assert err.value.translation_key == "soloist_pairing_required"
    # ... and the provider is taken out of service, so the user is asked to redo setup
    unload_with_error.assert_called_once()


async def test_a_login_that_never_happened_is_not_confused_with_another_failure(
    tmp_path: Path,
) -> None:
    """An unrelated failure keeps its own message even before any login was reported."""
    session = _make_session(tmp_path)
    session._fail("the capture sink was lost mid-stream")
    with pytest.raises(AudioError, match="capture sink was lost"):
        session._raise_startup_error("exited before playback started", TRACK_A)


def test_a_seeked_item_only_expects_what_is_left_of_it(tmp_path: Path) -> None:
    """A seeked item delivers the remainder, so its targets are based on that."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.duration_ms = 200_000
    full = 200_000 * CAPTURE_SAMPLE_RATE // 1000 * _FRAME_BYTES
    assert item._duration_bytes() == full
    item.seek_target_ms = 150_000
    remainder = 50_000 * CAPTURE_SAMPLE_RATE // 1000 * _FRAME_BYTES
    assert item._duration_bytes() == remainder
    # so the tail drain has a target it can actually reach
    item.start_tail_drain()
    item.write(b"\x01" * remainder)
    assert item.tail_complete is True
    # and the padding silence after it is refused
    item.write(b"\x00" * 4096)
    assert item.buffered == remainder


def test_the_lead_trim_never_exceeds_its_budget() -> None:
    """Silence beyond the budget is content, including where audio starts mid-chunk."""
    budget = int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND)
    # already at the budget, with a chunk whose silence runs well past it
    chunk = b"\x00" * 4096 + b"\x01" * 64
    trimmed, skipped = _trim_lead_silence(chunk, budget - _FRAME_BYTES)
    assert skipped == _FRAME_BYTES
    assert len(trimmed) == len(chunk) - _FRAME_BYTES


async def test_a_dying_log_reader_fails_the_session(tmp_path: Path) -> None:
    """Nothing else drains the daemon's stdout, so a dead reader must not go unnoticed."""
    session = _make_session(tmp_path)

    async def _boom() -> None:
        raise RuntimeError("reader blew up")

    session._log_task = asyncio.create_task(_boom())
    session._log_task.add_done_callback(session._task_done)
    await asyncio.sleep(0)
    await _wait_for(lambda: not session.usable)
    assert session._error is not None
    assert "reader blew up" in session._error


async def test_feeding_never_replaces_a_channel_already_in_use(tmp_path: Path) -> None:
    """If the engine reaches the fed item first, its live channel must survive."""
    session = _make_session(tmp_path, queue_id="player1")
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = _queue_item(TRACK_B)

    async def _engine_gets_there_first(_uri: str, **_kwargs: Any) -> None:
        # the events task advances to the fed item while the command is in flight
        await session._observe_current(TRACK_B, 200_000)

    _client_of(session).add_to_queue.side_effect = _engine_gets_there_first
    await session.feed_after(streamdetails, TRACK_A)
    live = session.current
    assert live is not None
    assert live.uri == TRACK_B
    # the channel the reader is writing to is the one a stream will be handed
    assert session._items[TRACK_B] is live
    assert session.item_for(TRACK_B) is live
    # and it is not queued as pending, because it already started
    assert session.has_pending is False


@pytest.mark.parametrize("other_queue", ["player2", "player1"])
async def test_a_session_in_use_is_never_cut_short(tmp_path: Path, other_queue: str) -> None:
    """
    An item the session cannot serve must not stop one it is still delivering.

    Covers a second player, and an early fetch across a boundary the session does
    not drive (a podcast episode, or the same track twice in a row) on the same
    queue. Reported as capacity, so a speculative prepare gives up softly.
    """
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.started.set()
    item.claim()
    with pytest.raises(ProviderStreamLimitError):
        await backend._acquire(TRACK_B, 0, other_queue)
    # the session that was playing is untouched
    assert backend._session is session
    assert session.usable is True


async def test_a_session_nobody_reads_is_replaced_for_another_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the other item has been released, the same request gets the session."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.started.set()
    item.claim()
    item.close()
    item.release()
    _install_fake_binary_manager(monkeypatch)
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    monkeypatch.setattr(session, "stop", AsyncMock())
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_B, 0, "player1")


async def test_a_replacement_waits_for_the_old_daemon_to_be_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine refuses to start while another daemon still holds its data dir."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    order: list[str] = []

    async def _slow_stop() -> None:
        order.append("stop-start")
        await asyncio.sleep(0.05)
        order.append("stop-done")

    monkeypatch.setattr(session, "stop", _slow_stop)
    _install_fake_binary_manager(monkeypatch)

    async def _spawn(_self: Any, _uri: str, _seek: int) -> None:
        order.append("spawn")
        raise AudioError("spawn")

    monkeypatch.setattr(soloist_backend._SoloistSession, "start", _spawn)
    # the session failed, so its teardown is under way when the next item arrives
    discard = asyncio.create_task(backend.discard_session(session))
    await asyncio.sleep(0)
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_B, 0, "player1")
    await discard
    assert order == ["stop-start", "stop-done", "spawn"]


async def test_an_idle_session_is_taken_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session nobody is reading is replaced instead of blocking another player."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    backend._session = session
    stopped = AsyncMock()
    monkeypatch.setattr(session, "stop", stopped)
    _install_fake_binary_manager(monkeypatch)
    # the replacement spawn is out of scope here; only the takeover decision is
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_B, 0, "player2")
    stopped.assert_awaited_once()


def test_a_dead_session_task_fails_the_session(tmp_path: Path) -> None:
    """A session task that dies of an unexpected error takes the session with it."""
    session = _make_session(tmp_path)
    task: Any = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("reader blew up")
    session._task_done(task)
    assert session.usable is False
    assert session._error is not None
    assert "reader blew up" in session._error


def test_a_cancelled_session_task_is_not_a_failure(tmp_path: Path) -> None:
    """Teardown cancels the session's tasks; that must not be reported as an error."""
    session = _make_session(tmp_path)
    task: Any = MagicMock()
    task.cancelled.return_value = True
    session._task_done(task)
    assert session.usable is True


async def test_failed_sink_control_fails_the_session(tmp_path: Path) -> None:
    """A failed suspend/resume fails the session instead of leaking stall silence."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._sink_running = True
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    session._pending.append(TRACK_B)
    _sink_of(session).suspend.side_effect = RuntimeError("pactl failed")
    await session._handle_event(_playback_event("buffering"))
    assert session._error is not None
    assert "capture sink control failed" in session._error


async def test_app_pause_is_fought_with_a_resume(tmp_path: Path) -> None:
    """A pause from the Spotify app is undone: this session has no user-facing pause."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    session._pending.append(TRACK_B)
    await session._handle_event(_playback_event("paused"))
    _client_of(session).resume.assert_awaited_once()


async def test_app_volume_change_is_pinned_back_to_unity(tmp_path: Path) -> None:
    """An off-unity volume set from the Spotify app is pinned back to 100."""
    session = _make_session(tmp_path)
    await session._handle_event(
        SoloistEvent(type="volume_changed", data=SoloistVolumeChanged(volume=40), raw={})
    )
    _client_of(session).set_volume.assert_awaited_once_with(100)
    _client_of(session).set_volume.reset_mock()
    await session._handle_event(
        SoloistEvent(type="volume_changed", data=SoloistVolumeChanged(volume=100), raw={})
    )
    _client_of(session).set_volume.assert_not_awaited()


async def test_track_change_signals_the_queue_when_it_matches_the_next_item(
    tmp_path: Path,
) -> None:
    """Reaching a fed item tells the queue to start filling that item's buffer."""
    session = _make_session(tmp_path, queue_id="player1")
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    session._items[TRACK_B] = _ItemAudio(TRACK_B, session)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(next_item=_queue_item(TRACK_B), current_index=0)
    await session._handle_event(
        SoloistEvent(
            type="track_changed",
            data=SoloistTrackChanged(item=SoloistEntity(uri=TRACK_B, entity_type="track")),
            raw={},
        )
    )
    queues.prepare_next_audio_buffer.assert_called_once_with("player1")


async def test_track_change_to_another_item_signals_nothing(tmp_path: Path) -> None:
    """An item the queue is not asking for next must not trigger a prebuffer."""
    session = _make_session(tmp_path, queue_id="player1")
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(next_item=_queue_item(TRACK_B), current_index=0)
    await session._handle_event(
        SoloistEvent(
            type="track_changed",
            data=SoloistTrackChanged(
                item=SoloistEntity(uri="spotify:track:surprise", entity_type="track")
            ),
            raw={},
        )
    )
    queues.prepare_next_audio_buffer.assert_not_called()


async def test_the_follower_of_the_streamed_item_is_fed(tmp_path: Path) -> None:
    """The item after the one being streamed is handed to the engine."""
    session = _make_session(tmp_path, queue_id="player1")
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    follower = _queue_item(TRACK_B)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=3)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 3 else None
    queues.get_next_item.return_value = follower
    await session.feed_after(streamdetails, TRACK_A)
    _client_of(session).add_to_queue.assert_awaited_once_with(TRACK_B)
    assert TRACK_B in session._items
    assert session.has_pending is True


async def test_an_item_the_queue_resolved_elsewhere_is_not_fed(tmp_path: Path) -> None:
    """A track the queue will stream from another provider must not be queued here."""
    session = _make_session(tmp_path, queue_id="player1")
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    # same track, but the queue already picked a different provider for it
    follower = _queue_item(TRACK_B, streamdetails=MagicMock(provider="tidal--x"))
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = follower
    await session.feed_after(streamdetails, TRACK_A)
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_a_fed_item_the_engine_has_not_reached_is_not_served(tmp_path: Path) -> None:
    """Skipping to an already-fed item must not hand over a channel that fills later."""
    session = _make_session(tmp_path)
    fed = session._items[TRACK_B] = _ItemAudio(TRACK_B, session)
    # fed, but the engine is still on the previous track
    assert session.item_for(TRACK_B) is None
    # once the engine gets there it is servable
    await session._observe_current(TRACK_B, 200_000)
    assert session.item_for(TRACK_B) is fed


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


async def test_only_whole_sample_frames_are_handed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that ends mid-frame must not split a frame across two items."""
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    item.started.set()
    item.claim()
    session._demand_started = True
    session._sink_running = True
    # two reads that are each mis-aligned but whole together
    reads = [b"\x01" * (_FRAME_BYTES + 3), b"\x02" * (_FRAME_BYTES - 3), b""]
    reader = MagicMock()

    async def _read(_size: int) -> bytes:
        return reads.pop(0) if reads else b""

    reader.read = _read
    session._reader = reader
    monkeypatch.setattr(soloist_backend, "_PACE_RATE", 1000.0)
    await session._read_capture()
    # every write was frame-aligned, and no byte was lost
    assert item.buffered % _FRAME_BYTES == 0
    assert item.buffered == _FRAME_BYTES * 2


async def test_an_already_known_item_is_not_fed_twice(tmp_path: Path) -> None:
    """An item the session already plays or was fed is not queued again."""
    session = _make_session(tmp_path, queue_id="player1")
    session._items[TRACK_B] = _ItemAudio(TRACK_B, session)
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = _queue_item(TRACK_B)
    await session.feed_after(streamdetails, TRACK_A)
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_only_tracks_are_fed_ahead(tmp_path: Path) -> None:
    """A podcast episode or audiobook chapter is played on its own, never stitched."""
    session = _make_session(tmp_path, queue_id="player1")
    await session.feed_after(MagicMock(), "spotify:episode:xyz")
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_a_non_spotify_follower_is_not_fed(tmp_path: Path) -> None:
    """The run simply ends where the queue leaves this provider."""
    session = _make_session(tmp_path, queue_id="player1")
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    follower = MagicMock(
        media_item=MagicMock(media_type=MediaType.TRACK, provider="tidal--x"), streamdetails=None
    )
    follower.media_item.provider_mappings = []
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = follower
    await session.feed_after(streamdetails, TRACK_A)
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_a_library_item_is_fed_through_its_spotify_mapping(tmp_path: Path) -> None:
    """A library track is fed with the item id this provider instance knows it by."""
    session = _make_session(tmp_path, queue_id="player1")
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    follower = MagicMock(
        media_item=MagicMock(media_type=MediaType.TRACK, provider="library", item_id="42"),
        streamdetails=None,
    )
    follower.media_item.provider_mappings = [
        MagicMock(provider_instance="other--y", item_id="wrong"),
        MagicMock(provider_instance="spotify--test", item_id="bbb"),
    ]
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = follower
    await session.feed_after(streamdetails, TRACK_A)
    _client_of(session).add_to_queue.assert_awaited_once_with(TRACK_B)


def test_crossfade_comes_from_the_queue_preference(tmp_path: Path) -> None:
    """The queue's crossfade setting is handed to the engine, in milliseconds."""
    session = _make_session(tmp_path, queue_id="player1")
    _queues_of(session).get.return_value = MagicMock(queue_id="player1", crossfade_enabled=True)
    cast("MagicMock", session.mass.config).get_raw_core_config_value = MagicMock(return_value=6)
    assert session._queue_crossfade_ms() == 6000


def test_crossfade_off_is_zero(tmp_path: Path) -> None:
    """A queue with crossfade disabled gets an explicit zero (which clears the pref)."""
    session = _make_session(tmp_path, queue_id="player1")
    _queues_of(session).get.return_value = MagicMock(crossfade_enabled=False)
    assert session._queue_crossfade_ms() == 0


def test_no_queue_means_no_crossfade(tmp_path: Path) -> None:
    """Without a queue to read the preference from, the engine gets no crossfade."""
    session = _make_session(tmp_path, queue_id=None)
    assert session._queue_crossfade_ms() == 0


async def test_short_delivery_is_rejected_as_incomplete(tmp_path: Path) -> None:
    """PCM that stops well short of the item's duration is rejected."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.playing_seen = True
    item.duration_ms = 200_000
    item.last_position_ms = 100_000
    with pytest.raises(AudioError, match="incomplete"):
        await session.validate_item(item)


async def test_a_crossfade_shortfall_is_tolerated(tmp_path: Path) -> None:
    """With crossfade the engine reports the item short by design; that is not a failure."""
    session = _make_session(tmp_path)
    session.crossfade_ms = 12_000
    item = _ItemAudio(TRACK_A, session)
    item.playing_seen = True
    item.duration_ms = 200_000
    # 12s of crossfade plus the ordinary tolerance
    item.last_position_ms = 200_000 - 21_000
    await session.validate_item(item)


async def test_missing_position_is_rejected_as_incomplete(tmp_path: Path) -> None:
    """Without any position report there is no evidence the item played out."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.playing_seen = True
    item.duration_ms = 200_000
    with pytest.raises(AudioError, match="incomplete"):
        await session.validate_item(item)


async def test_short_item_cannot_pass_at_position_zero(tmp_path: Path) -> None:
    """The tolerance never spans a whole item, so a short item cannot pass unplayed."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.playing_seen = True
    item.duration_ms = 8_000
    item.last_position_ms = 0
    with pytest.raises(AudioError, match="incomplete"):
        await session.validate_item(item)


async def test_an_item_that_never_played_is_rejected(tmp_path: Path) -> None:
    """An item the engine never reported playing is a failure, whatever was delivered."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.duration_ms = 200_000
    item.last_position_ms = 200_000
    with pytest.raises(AudioError, match="never started playing"):
        await session.validate_item(item)


async def test_a_duration_less_item_is_not_judged(tmp_path: Path) -> None:
    """Without a duration there is nothing to judge completeness against."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.playing_seen = True
    await session.validate_item(item)


def test_an_unread_session_expires(tmp_path: Path) -> None:
    """A session no item stream reads from is ended so its daemon does not linger."""
    session = _make_session(tmp_path)
    session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    session._expire_idle()
    assert session._idle_since is not None
    assert session.usable is True
    session._idle_since = time.monotonic() - _IDLE_TIMEOUT_S - 1
    session._expire_idle()
    assert session.usable is False


def test_a_session_being_read_never_expires(tmp_path: Path) -> None:
    """An item stream reading the session keeps it alive indefinitely."""
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.claim()
    session._idle_since = time.monotonic() - _IDLE_TIMEOUT_S * 10
    session._expire_idle()
    assert session.usable is True


def test_a_failed_session_is_torn_down(tmp_path: Path) -> None:
    """A session that fails is discarded, so its daemon does not keep playing to nobody."""
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.claim()
    session._fail("audio stalled")
    assert session.usable is False
    # every waiting item is released and the teardown is scheduled
    assert item._closed is True
    # a startup wait must not sit out its timeout on a session that already failed
    assert item.started.is_set() is True
    discard = cast("MagicMock", session.mass.create_task)
    discard.assert_called_once_with(session.backend.discard_session, session)
    # a second failure does not queue a second teardown
    session._fail("and again")
    assert session._error == "audio stalled"
    assert discard.call_count == 1


async def test_an_item_the_engine_skipped_past_fails_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed channel the engine never reaches gives up rather than blocking forever."""
    monkeypatch.setattr(soloist_backend, "_READ_SLICE_S", 0.01)
    monkeypatch.setattr(soloist_backend, "_STALL_TIMEOUT_S", 0.05)
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.claim()
    # the engine is playing something else, so nothing is ever written here
    session._items["spotify:track:other"] = session._current = _ItemAudio(
        "spotify:track:other", session
    )
    with pytest.raises(AudioError, match="no audio"):
        async for _ in item.read():
            pass


async def test_adopt_paired_session_copies_into_the_canonical_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session paired by the setup flow is adopted into the per-instance data dir."""
    storage = tmp_path / "storage"
    pending = storage / "spotify" / "pairing" / "flow1"
    pending.mkdir(parents=True)
    (pending / "session.bin").write_bytes(b"session")
    prov = _make_provider(tmp_path, {CONF_SOLOIST_SESSION_DIR: "spotify/pairing/flow1"})
    update_setup_data = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update_setup_data)
    backend = SoloistBackend(prov)
    await backend._adopt_paired_session()
    canonical = storage / "spotify" / "spotify--test" / SOLOIST_DATA_DIR_NAME
    assert (canonical / "session.bin").read_bytes() == b"session"
    # a copy, not a move: the flow-private source must survive a failed
    # provider load so the setup flow can retry (the flow removes it at its end)
    assert (pending / "session.bin").exists()
    update_setup_data.assert_called_once_with(CONF_SOLOIST_SESSION_DIR, None)


def test_the_engine_is_told_not_to_normalize(tmp_path: Path) -> None:
    """MA normalizes this audio itself, so the engine's own normalization is switched off."""
    backend = _make_backend(tmp_path)
    prefs = backend._data_dir / "settings" / "Users" / "alice-user" / "prefs"
    prefs.parent.mkdir(parents=True)
    prefs.write_text("some.engine.key=1\n", encoding="utf-8")
    backend._prepare_data_dir(8000)
    content = prefs.read_text(encoding="utf-8").splitlines()
    assert "some.engine.key=1" in content
    assert "audio.normalize_v2=false" in content
    assert "audio.crossfade_v2=true" in content
    assert "audio.crossfade.time_v2=8000" in content
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
    backend._prepare_data_dir(0)
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
    """Only a dir holding something besides the WS endpoint files counts as paired."""
    data_dir = tmp_path / "soloist-data"
    assert soloist_session_present(data_dir) is False
    data_dir.mkdir()
    (data_dir / WS_ADDR_FILE).write_text("127.0.0.1", encoding="utf-8")
    (data_dir / WS_PORT_FILE).write_text("1234", encoding="utf-8")
    assert soloist_session_present(data_dir) is False
    (data_dir / "session.bin").write_bytes(b"x")
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


def _make_session(tmp_path: Path, queue_id: str | None = "player1") -> _SoloistSession:
    """Return a session with its process/sink/client replaced by mocks."""
    session = _SoloistSession(_make_backend(tmp_path), queue_id)
    session._sink = AsyncMock()
    session._client = AsyncMock()
    session._proc = MagicMock(returncode=None)
    # a session under test is past the engine's login unless a test says otherwise
    session._logged_in = True
    return session


def _make_item(tmp_path: Path, uri: str) -> _ItemAudio:
    """Return a bare item channel on a mocked session."""
    return _ItemAudio(uri, _make_session(tmp_path))


def _queue_item(uri: str, streamdetails: Any = None) -> MagicMock:
    """Return a queue item stand-in for a Spotify track on the test instance."""
    item_id = uri.rsplit(":", 1)[1]
    media_item = MagicMock(media_type=MediaType.TRACK, provider="spotify--test", item_id=item_id)
    media_item.provider_mappings = []
    return MagicMock(
        media_item=media_item,
        queue_item_id=f"qi-{item_id}",
        streamdetails=streamdetails,
    )


async def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait until the predicate holds, so a background task can get there."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _client_of(session: _SoloistSession) -> AsyncMock:
    """Return the session's mocked WebSocket client."""
    return cast("AsyncMock", session._client)


def _sink_of(session: _SoloistSession) -> AsyncMock:
    """Return the session's mocked capture sink."""
    return cast("AsyncMock", session._sink)


def _queues_of(session: _SoloistSession) -> MagicMock:
    """Return the mocked player_queues controller the session consults."""
    return cast("MagicMock", session.mass.player_queues)


def _auth_event(*, logged_in: bool) -> SoloistEvent:
    """Return an auth_state event with the given login state."""
    return SoloistEvent(
        type="auth_state",
        data=SoloistAuthState(logged_in=logged_in, is_active=False),
        raw={},
    )


def _playback_event(status: str, position_ms: int = 0) -> SoloistEvent:
    """Return a playback_state event for the current item with the given status."""
    return SoloistEvent(
        type="playback_state",
        data=SoloistPlaybackState(
            status=status,
            item=SoloistEntity(uri=TRACK_A, entity_type="track"),
            position=SoloistPosition(position_ms=position_ms, timestamp_ms=0),
        ),
        raw={},
    )


def _install_fake_binary_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the shared binary manager so no download or exec is attempted."""
    manager = MagicMock()
    manager.ensure_fresh = AsyncMock(return_value=Path("/nonexistent/soloist"))
    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", MagicMock(return_value=manager))
