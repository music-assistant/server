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
import os
import time
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import AudioError, LoginFailed
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio_buffer import BUFFER_READY_TIMEOUT
from music_assistant.helpers.config_entries import (
    PUBLISH_NAME_TEMPLATES,
    resolve_publish_name,
)
from music_assistant.helpers.pulse_capture import CAPTURE_SAMPLE_RATE
from music_assistant.models.music_provider import ProviderStreamLimitError
from music_assistant.providers.spotify.backends import StreamSupersededError
from music_assistant.providers.spotify.backends import soloist as soloist_backend
from music_assistant.providers.spotify.backends.soloist import (
    _BYTES_PER_SECOND,
    _FRAME_BYTES,
    _IDLE_TIMEOUT_S,
    _ITEM_OVERRUN_S,
    _JUMP_TIMEOUT_S,
    _MAX_APP_PAUSE_RESUMES,
    _MAX_LEAD_TRIM_S,
    _READ_CHUNK_SIZE,
    SoloistAppControl,
    SoloistAppControlError,
    SoloistBackend,
    _CaptureShaper,
    _ItemAudio,
    _SoloistSession,
    _trim_lead_silence,
)
from music_assistant.providers.spotify.constants import (
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    SOLOIST_DATA_DIR_NAME,
    SOLOIST_DEVICE_NAME,
)
from music_assistant.providers.spotify.helpers import soloist_session_present
from music_assistant.providers.spotify.provider import SpotifyProvider
from music_assistant.providers.spotify_connect.soloist.runtime import (
    WS_ADDR_FILE,
    WS_PORT_FILE,
    SoloistAuthState,
    SoloistDeviceChanged,
    SoloistEntity,
    SoloistError,
    SoloistEvent,
    SoloistOptionsChanged,
    SoloistPlaybackOptions,
    SoloistPlaybackState,
    SoloistPosition,
    SoloistPositionSync,
    SoloistTrackChanged,
    SoloistVolumeChanged,
)

TRACK_A = "spotify:track:aaa"
TRACK_B = "spotify:track:bbb"
TRACK_C = "spotify:track:ccc"
# an audiobook is one item stitched from a Spotify URI per chapter
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


def test_seek_is_confirmed_only_within_tolerance(tmp_path: Path) -> None:
    """A position report confirms a seek only once it reaches the tolerance window."""
    item = _make_item(tmp_path, TRACK_A)
    item.arm_seek(60_000)
    item.observe_position(50_000)
    assert not item.seek_confirmed.is_set()
    item.observe_position(58_500)
    assert item.seek_confirmed.is_set()
    assert item.started_at_ms == 58_500


def test_small_seek_target_is_not_confirmed_by_a_pre_seek_zero_report(tmp_path: Path) -> None:
    """A position-0 report before the seek lands cannot confirm a small target."""
    item = _make_item(tmp_path, TRACK_A)
    item.arm_seek(1_500)
    item.observe_position(0)
    assert not item.seek_confirmed.is_set()
    item.observe_position(1_500)
    assert item.seek_confirmed.is_set()


def test_a_small_seek_is_confirmed_without_a_report_of_exactly_zero(tmp_path: Path) -> None:
    """A target inside the tolerance window has no room below it to be anchored on."""
    item = _make_item(tmp_path, TRACK_A)
    # the engine restored this item a second in, so the seek is short enough
    # that no report can fall below its tolerance window
    item.observe_position(1_200)
    item.arm_seek(2_000)
    item.observe_position(400)
    item.observe_position(2_000)
    assert item.seek_confirmed.is_set()


def test_the_restored_position_of_the_same_item_cannot_confirm_a_seek(tmp_path: Path) -> None:
    """The state a fresh session restores does not pass for the seek landing."""
    item = _make_item(tmp_path, TRACK_A)
    item.duration_ms = 176_000
    # the engine restores the account's last state: this very item, sitting at
    # the position the seek is aiming for
    item.observe_position(117_000)
    item.arm_seek(117_000)
    item.observe_position(117_000)
    assert not item.seek_confirmed.is_set()
    # only once the engine has reloaded the track does its seek count
    item.observe_position(0)
    item.observe_position(117_000)
    assert item.seek_confirmed.is_set()


def test_a_backward_seek_is_confirmed_below_where_the_engine_was(tmp_path: Path) -> None:
    """Seeking back into an item confirms on the target, not on where it came from."""
    item = _make_item(tmp_path, TRACK_A)
    # the engine restored this item well past the point being seeked back to
    item.observe_position(117_000)
    item.arm_seek(30_000)
    item.observe_position(0)
    item.observe_position(30_000)
    assert item.seek_confirmed.is_set()
    assert item.started_at_ms == 30_000


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
    item_a = session._open_channel(TRACK_A)
    session._current = item_a
    item_a.started.set()
    item_a.claim()
    item_a.write(b"a" * 16)
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    item_a.write(b"late" * 4)  # written after the cut: goes nowhere
    chunks = [chunk async for chunk in item_a.read()]
    assert b"".join(chunks) == b"a" * 16
    # the next item exists, carries the duration and now receives the audio
    item_b = session.current
    assert item_b is not None
    assert item_b.uri == TRACK_B
    assert item_b.duration_ms == 200_000


async def test_the_engines_restored_state_does_not_cut_a_pending_item(
    tmp_path: Path,
) -> None:
    """A daemon reports the item it restored before playing ours; that is not a boundary."""
    session = _make_session(tmp_path)
    requested = session._open_channel(TRACK_A)
    session._current = requested
    requested.claim()
    # the engine announces the state it came up with, which is someone else's item
    await session._observe_current("spotify:track:restored", 152_000, track_changed=False)
    # closing our item here would end its stream before it delivered anything
    assert requested._closed is False
    assert requested.started.is_set() is False
    # ... and the restored item is never offered as an item's audio
    assert session.item_for("spotify:track:restored") is None
    # then ours starts for real, and picks up from there
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    assert session.current is requested
    assert requested.started.is_set() is True
    requested.write(b"\x01" * 32)
    requested.close()
    assert b"".join([chunk async for chunk in requested.read()]) == b"\x01" * 32


async def test_leaving_the_engines_restored_item_is_not_a_takeover(tmp_path: Path) -> None:
    """The restored item is part-way through a track, and we are about to leave it."""
    session = _make_session(tmp_path)
    # as _play leaves it: the channel exists, its stream is not reading it yet
    requested = session._open_channel(TRACK_A)
    session._current = requested
    await session._observe_current("spotify:track:restored", 152_000, track_changed=False)
    restored = session.current
    assert restored is not None
    restored.observe_position(20_000)

    # our own play() lands and the engine leaves the restored item for ours
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    assert session.usable is True
    assert session.current is requested


async def test_audio_read_before_the_stream_opens_is_kept(tmp_path: Path) -> None:
    """Audio captured before an item's stream opens is buffered, not dropped."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    session._current = item
    item.write(b"head" * 8)
    item.claim()
    item.close()
    chunks = [chunk async for chunk in item.read()]
    assert b"".join(chunks) == b"head" * 8


async def test_a_channel_is_only_ever_served_once(tmp_path: Path) -> None:
    """A consumed channel cannot be replayed, so the item needs a fresh session."""
    session = _make_session(tmp_path)
    item = session._current = session._open_channel(TRACK_A)
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
    item = session._current = session._open_channel(TRACK_A)
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
    session._was_active = False
    await session._handle_event(_auth_event(logged_in=False, is_active=False))
    # failing here would break every playback on a perfectly good pairing
    assert session.usable is True
    await session._handle_event(_auth_event(logged_in=True, is_active=False))
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
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
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
    session._current = session._open_channel(TRACK_A)
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
    item = session._current = session._open_channel(TRACK_A)
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
    item = session._current = session._open_channel(TRACK_A)
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
    item = session._current = session._open_channel(TRACK_A)
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
    item = session._current = session._open_channel(TRACK_A)
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
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
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


async def test_a_takeover_between_activate_and_play_stops_the_start(tmp_path: Path) -> None:
    """Playing here would claim the device straight back off wherever the user moved to."""
    session = _make_session(tmp_path)
    session._was_active = False
    client = _client_of(session)

    async def _take_over(*_args: Any, **_kwargs: Any) -> None:
        session._observe_active_device(is_active=False)

    client.set_repeat_track.side_effect = _take_over
    ready = asyncio.Event()
    ready.set()
    with pytest.raises(SoloistAppControlError):
        await session._play(TRACK_A, 0, ready)
    client.play.assert_not_awaited()


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
    await session._log_output(
        _stdout_of(
            'Error: another session is running for data directory "/data/x/soloist-data".',
            "Stop the running session before starting soloist again.",
        )
    )
    assert session._data_dir_busy is True


async def test_a_lost_pairing_is_caught_the_moment_the_daemon_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon advertising for pairing fails the session at once, not on a timeout."""
    session = _make_session(tmp_path)
    session._logged_in = None
    unload_with_error = MagicMock()
    monkeypatch.setattr(session.backend.provider, "unload_with_error", unload_with_error)
    await session._log_output(
        _stdout_of('waiting for login - connect to "X" from your Spotify app')
    )
    assert session._unpaired is True
    # the buffer gives up on the audio long before the startup budget runs out, so
    # the session has to fail while an item is still waiting on it
    assert session._error is not None
    with pytest.raises(LoginFailed) as err:
        session._raise_startup_error("did not connect and log in", TRACK_A)
    assert err.value.translation_key == "soloist_pairing_required"
    unload_with_error.assert_called_once()


async def test_a_lost_pairing_fails_the_item_without_waiting_for_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The item fails on the lost pairing, not on the endpoint that is no longer coming."""
    session = _make_session(tmp_path)
    session._logged_in = None
    monkeypatch.setattr(session.backend.provider, "unload_with_error", MagicMock())
    await session._log_output(
        _stdout_of('waiting for login - connect to "X" from your Spotify app')
    )
    # the endpoint never appears, so the wait for it must not swallow the failure:
    # sitting it out would outlast the queue's own patience for the audio
    with pytest.raises(LoginFailed) as err:
        async with asyncio.timeout(5):
            await session._play(TRACK_A, 0, asyncio.Event())
    assert err.value.translation_key == "soloist_pairing_required"


async def test_a_daemon_still_restoring_its_session_is_left_alone(tmp_path: Path) -> None:
    """The engine advertises for pairing while restoring too; the stored session decides."""
    session = _make_session(tmp_path)
    data_dir = session.backend._data_dir
    (data_dir / "settings" / "Users" / "spotify-user-user").mkdir(parents=True)
    await session._log_output(
        _stdout_of('waiting for login - connect to "X" from your Spotify app')
    )
    assert session._unpaired is False
    assert session._error is None


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


@pytest.mark.parametrize(
    ("target_ms", "reports"),
    [
        # seeking the restored item to where the engine already was
        (117_000, (117_000, 0, 117_000)),
        # seeking back into it, where every report lands below where it was
        (30_000, (117_000, 0, 30_000)),
    ],
)
async def test_the_seek_retries_until_the_engine_reports_the_target(
    tmp_path: Path, target_ms: int, reports: tuple[int, ...]
) -> None:
    """A seek dropped while the track loads is re-sent until a report confirms it."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    client = cast("Any", session._client)
    # the engine restored this item part-way in, before the seek goes out
    item.observe_position(117_000)

    async def _report_positions() -> None:
        for position_ms in reports:
            await asyncio.sleep(0)
            item.observe_position(position_ms)

    with (
        patch.object(soloist_backend, "_SEEK_RETRY_INTERVAL_S", 0.01),
        # bounded so a regression fails fast instead of sitting out the real budget
        patch.object(soloist_backend, "_SEEK_CONFIRM_TIMEOUT_S", 1.0),
    ):
        reporter = asyncio.create_task(_report_positions())
        await session._cold_seek(client, item, target_ms)
        await reporter
    assert item.seek_confirmed.is_set()
    assert item.started_at_ms == target_ms
    assert client.seek.await_count >= 1


async def test_a_seek_that_only_ever_sees_the_restored_position_fails(tmp_path: Path) -> None:
    """A seek nothing confirms fails loudly rather than streaming from elsewhere."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    # the engine sits at the restored position and never reloads the track
    item.observe_position(117_000)
    with (
        patch.object(soloist_backend, "_SEEK_RETRY_INTERVAL_S", 0.01),
        patch.object(soloist_backend, "_SEEK_CONFIRM_TIMEOUT_S", 0.05),
        pytest.raises(AudioError, match="did not confirm seeking"),
    ):
        await session._cold_seek(cast("Any", session._client), item, 117_000)


async def test_a_seek_the_engine_ignored_does_not_cut_the_item_short(tmp_path: Path) -> None:
    """An item the engine plays from its start is bounded by its full duration."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.duration_ms = 176_000
    item.arm_seek(117_000)
    item.claim()
    # the engine never made the seek and is playing the item from its start, so
    # the audio it delivers runs well past what the seeked remainder would allow
    item.observe_position(80_000)
    item.write(b"\x01" * (89 * _BYTES_PER_SECOND))
    item.close()
    delivered = 0
    async for chunk in item.read():
        delivered += len(chunk)
    assert delivered == 89 * _BYTES_PER_SECOND


async def test_a_seek_that_landed_still_bounds_the_item_at_its_remainder(tmp_path: Path) -> None:
    """An item the engine really seeked into stays bounded by what is left of it."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.duration_ms = 176_000
    item.arm_seek(117_000)
    item.observe_position(0)
    item.observe_position(117_000)
    assert item.seek_confirmed.is_set()
    assert item._overrun_limit() == 59 * _BYTES_PER_SECOND + int(
        _ITEM_OVERRUN_S * _BYTES_PER_SECOND
    )
    # later reports do not move the latch, which would shrink the bound
    item.observe_position(150_000)
    assert item.started_at_ms == 117_000
    # so it still fails once it runs that far past the seek point
    item.claim()
    item.write(b"\x01" * (89 * _BYTES_PER_SECOND))
    with pytest.raises(AudioError, match="never moved on"):
        async for _ in item.read():
            pass


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
    streamed = _streamed(session)
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = _queue_item(TRACK_B)

    async def _engine_gets_there_first(_uri: str, **_kwargs: Any) -> None:
        # the events task advances to the fed item while the command is in flight
        await session._observe_current(TRACK_B, 200_000, track_changed=True)

    _client_of(session).add_to_queue.side_effect = _engine_gets_there_first
    await session.feed_after(streamdetails, streamed)
    live = session.current
    assert live is not None
    assert live.uri == TRACK_B
    # the channel the reader is writing to is the one a stream will be handed
    assert [item for item in session._channels if item.uri == TRACK_B] == [live]
    assert session.item_for(TRACK_B) is live
    # and it is not queued as pending, because it already started
    assert session.has_pending is False


async def test_a_seek_the_session_cannot_take_restarts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A seek the running session cannot serve falls back to restarting it.

    A realtime source has not captured anything past the play position, so any
    forward seek lands outside the buffer and comes back here; the session is
    seeked in place when it can be, and replaced when it cannot.
    """
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    # its own stream is still attached when the seek re-opens it
    item.claim()
    stopped = AsyncMock()
    monkeypatch.setattr(session, "stop", stopped)
    _install_fake_binary_manager(monkeypatch)
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_A, 90, _streamdetails_for(queue_id="player1", uri=TRACK_A))
    stopped.assert_awaited_once()


@pytest.mark.parametrize(
    ("requested", "other_queue"),
    [
        # another player, whatever it asks for - including the very track this
        # session is in the middle of delivering
        pytest.param(TRACK_B, "player2", id="other_player"),
        pytest.param(TRACK_A, "player2", id="other_player_same_track"),
        # an early fetch across a boundary this session does not drive, such as a
        # podcast episode or audiobook chapter
        pytest.param(TRACK_B, "player1", id="unstitched_boundary"),
    ],
)
async def test_a_session_in_use_is_never_cut_short(
    tmp_path: Path, requested: str, other_queue: str
) -> None:
    """
    An item the session cannot serve must not stop one it is still delivering.

    Reported as capacity, so a speculative prepare gives up softly.
    """
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    item = session._open_channel(TRACK_A)
    item.started.set()
    item.claim(_streamdetails_for(uri=TRACK_A).uri)
    # the session really is playing TRACK_A, so a same-track request from another
    # player cannot be mistaken for a seek
    session._current = item
    with pytest.raises(ProviderStreamLimitError) as err:
        await backend._acquire(
            requested, 0, _streamdetails_for(queue_id=other_queue, uri=requested)
        )
    # a stream-limit error so the item is not marked unplayable, but the message
    # is about the session, not the provider's source-stream budget
    assert err.value.limit == 1
    assert err.value.translation_key == "soloist_session_busy"
    # the session that was playing is untouched
    assert backend._session is session
    assert session.usable is True


async def test_a_seek_across_an_audiobook_chapter_supersedes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A seek of the item being delivered restarts the session instead of being refused.

    An audiobook's chapters are separate Spotify URIs of one item, so a seek
    across a chapter boundary asks for a URI the engine is not on - which is
    still this queue's own seek, not another item after a busy session.
    """
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    book = _streamdetails_for(uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK)
    # the engine is on the first chapter, whose stream is still attached
    playing = _streamed(session, CHAPTER_A, media_key=book.uri)
    _install_fake_binary_manager(monkeypatch)
    # the replacement spawn is out of scope here; only the takeover decision is
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(CHAPTER_B, 90, book)
    # the session really was torn down, and the stream it was feeding is told
    # so rather than stitching the chapter after this one on
    assert session._stopped is True
    assert playing.superseded is True


@pytest.mark.parametrize(
    "rolled_into",
    [
        # the chapter after the one it streamed, wherever the seek went
        CHAPTER_C,
        # or the very chapter the seek landed on, which a forward seek makes the
        # likely one: the session is playing it, and seeking it in place would
        # hand this stale stream the live stream's channel
        CHAPTER_B,
    ],
    ids=["another_chapter", "the_seeked_chapter"],
)
async def test_a_chapter_rolled_into_after_a_seek_does_not_take_the_session_back(
    tmp_path: Path, rolled_into: str
) -> None:
    """
    The stream a seek replaced must not take the session back for its next chapter.

    A chapter boundary is a fresh session, so the stream that was replaced can
    arrive at one having released its own channel - by which point the session
    delivering this audiobook is the one that superseded it.
    """
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    backend._session = session
    book = _streamdetails_for(uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK)
    # the session the seek started, reading the chapter it landed on
    live = _streamed(session, CHAPTER_B, media_key=book.uri)
    with pytest.raises(StreamSupersededError):
        await backend._acquire(rolled_into, 0, book, continuation=True)
    # the seek stands, and the stream serving it still has its audio
    assert backend._session is session
    assert session.usable is True
    assert live.superseded is False
    assert live.claimed is True


async def test_a_chapter_boundary_that_finds_another_player_reports_capacity(
    tmp_path: Path,
) -> None:
    """Another player taking the session in that gap is capacity, not a supersede."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player2")
    backend._session = session
    # the other player started something of its own while this book was between
    # chapters, which is what its stream has to be told
    _streamed(session, TRACK_A, media_key=_streamdetails_for(queue_id="player2").uri)
    book = _streamdetails_for(queue_id="player1", uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK)
    with pytest.raises(ProviderStreamLimitError) as err:
        await backend._acquire(CHAPTER_B, 0, book, continuation=True)
    assert err.value.translation_key == "soloist_session_busy"


async def test_the_next_chapter_still_gets_the_session_its_stream_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary chapter boundary reads as nobody else's, so it is served as before."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    backend._session = session
    book = _streamdetails_for(uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK)
    # the chapter this stream just finished, whose channel it has let go
    _streamed(session, CHAPTER_A, media_key=book.uri).release()
    stopped = AsyncMock()
    monkeypatch.setattr(session, "stop", stopped)
    _install_fake_binary_manager(monkeypatch)
    # chapters are never fed ahead, so the next one is a fresh session either way
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(CHAPTER_B, 0, book, continuation=True)
    stopped.assert_awaited_once()


async def test_a_seek_of_another_queues_audiobook_is_still_refused(tmp_path: Path) -> None:
    """One queue's seek must not restart a session another player is listening to."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    book = _streamdetails_for(uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK)
    _streamed(session, CHAPTER_A, media_key=book.uri)
    # the same audiobook, played on another player
    elsewhere = _streamdetails_for(
        queue_id="player2", uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK
    )
    with pytest.raises(ProviderStreamLimitError):
        await backend._acquire(CHAPTER_B, 90, elsewhere)
    assert backend._session is session
    assert session.usable is True


def test_a_session_reading_two_items_is_not_delivering_only_one(tmp_path: Path) -> None:
    """A seek may only restart a session whose every stream is that item's own."""
    session = _make_session(tmp_path)
    book = _streamdetails_for(uri=AUDIOBOOK, media_type=MediaType.AUDIOBOOK).uri
    _streamed(session, CHAPTER_A, media_key=book)
    assert session.serves_only(book) is True
    # the previous item's channel is still being drained alongside it
    tail = session._open_channel(TRACK_A)
    tail.claim(_streamdetails_for(uri=TRACK_A).uri)
    assert session.serves_only(book) is False


def test_a_caller_without_details_never_matches_what_a_session_delivers(tmp_path: Path) -> None:
    """StreamDetails are the only way to tell a seek of the item being delivered."""
    session = _make_session(tmp_path)
    # a channel nothing attributed to a Music Assistant item
    _streamed(session, CHAPTER_A)
    assert session.serves_only(None) is False


@pytest.mark.parametrize(
    "playing_seen",
    [
        True,
        # a second seek cuts the first one's channel before the engine reports
        # it playing; the stream must still stop rather than be counted a
        # failure, which its caller answers by stitching the next part on
        False,
    ],
    ids=["was_playing", "never_started"],
)
async def test_a_stream_a_seek_cut_short_reports_itself_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, playing_seen: bool
) -> None:
    """
    A stream Music Assistant cut ends with an error of its own, not a clean end.

    Its caller stitches the item's next part onto a part that ended by itself;
    everything past the cut belongs to the stream that replaced this one.
    """
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _make_session(tmp_path)
    item = session._current = session._open_channel(CHAPTER_A)
    item.started.set()
    item.playing_seen = playing_seen
    item.claim()
    item.write(b"a" * 16)
    # the seek cuts the channel while its stream is still reading it
    item.close(superseded=True)

    async def _acquired(*_args: Any, **_kwargs: Any) -> tuple[_SoloistSession, _ItemAudio]:
        return session, item

    monkeypatch.setattr(backend, "_acquire", _acquired)
    chunks: list[bytes] = []

    async def _drain() -> None:
        async for chunk in backend.stream_spotify_uri(CHAPTER_A):
            chunks.append(chunk)

    with pytest.raises(StreamSupersededError):
        await _drain()
    # the audio it did capture is still handed over
    assert b"".join(chunks) == b"a" * 16


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


async def test_a_session_nobody_reads_is_replaced_for_another_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the other item has been released, the same request gets the session."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    item = session._open_channel(TRACK_A)
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
        await backend._acquire(TRACK_B, 0, _streamdetails_for(queue_id="player1", uri=TRACK_B))


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
        await backend._acquire(TRACK_B, 0, _streamdetails_for(queue_id="player1", uri=TRACK_B))
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
    session._open_channel(TRACK_A)
    backend._session = session
    stopped = AsyncMock()
    monkeypatch.setattr(session, "stop", stopped)
    _install_fake_binary_manager(monkeypatch)
    # the replacement spawn is out of scope here; only the takeover decision is
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_B, 0, _streamdetails_for(queue_id="player2", uri=TRACK_B))
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
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    _sink_of(session).suspend.side_effect = RuntimeError("pactl failed")
    await session._handle_event(_playback_event("buffering"))
    assert session._error is not None
    assert "capture sink control failed" in session._error


async def test_app_pause_is_fought_with_a_resume(tmp_path: Path) -> None:
    """A pause from the Spotify app is undone: this session has no user-facing pause."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    await session._handle_event(_playback_event("paused"))
    _client_of(session).resume.assert_awaited_once()


async def test_an_app_pause_is_only_undone_so_many_times(tmp_path: Path) -> None:
    """Someone who keeps pausing means it: the session gives up instead of fighting on."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    for _ in range(_MAX_APP_PAUSE_RESUMES):
        await session._handle_event(_playback_event("playing"))
        await session._handle_event(_playback_event("paused"))
    assert _client_of(session).resume.await_count == _MAX_APP_PAUSE_RESUMES
    assert session._error is None

    await session._handle_event(_playback_event("playing"))
    await session._handle_event(_playback_event("paused"))
    assert _client_of(session).resume.await_count == _MAX_APP_PAUSE_RESUMES
    assert session.usable is False
    assert session._app_control is SoloistAppControl.PAUSED


async def test_one_pause_reported_twice_counts_once(tmp_path: Path) -> None:
    """A repeated snapshot of the same pause is not a new pause."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    for _ in range(_MAX_APP_PAUSE_RESUMES + 2):
        await session._handle_event(_playback_event("paused"))
    assert session.usable is True


async def test_the_pause_budget_resets_on_the_next_item(tmp_path: Path) -> None:
    """Each item gets its own budget; pausing one track does not spend the next one's."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    for _ in range(_MAX_APP_PAUSE_RESUMES):
        await session._handle_event(_playback_event("playing"))
        await session._handle_event(_playback_event("paused"))
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    assert session._app_pauses == 0


async def test_a_pause_is_not_undone_once_the_device_is_gone(tmp_path: Path) -> None:
    """A bare resume on a device Spotify no longer routes to would play to nobody."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._was_active = False
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    await session._handle_event(_playback_event("paused"))
    _client_of(session).resume.assert_not_awaited()


async def test_losing_the_active_device_ends_the_session(tmp_path: Path) -> None:
    """Playback moved to another device from the Spotify app: this session is over."""
    session = _make_session(tmp_path)
    await session._handle_event(_device_event(is_active=False))
    assert session.usable is False
    assert session._app_control is SoloistAppControl.TOOK_OVER


async def test_a_takeover_reported_on_the_auth_state_ends_the_session(tmp_path: Path) -> None:
    """The active-device state also rides on auth_state, and counts the same there."""
    session = _make_session(tmp_path)
    await session._handle_event(_auth_event(logged_in=True, is_active=False))
    assert session.usable is False


async def test_an_inactive_device_before_activation_is_not_a_takeover(tmp_path: Path) -> None:
    """A fresh daemon is inactive until the session claims it; that is not a takeover."""
    session = _make_session(tmp_path)
    session._was_active = False
    await session._handle_event(_device_event(is_active=False))
    await session._handle_event(_auth_event(logged_in=True, is_active=False))
    assert session.usable is True

    # nor does a respawned daemon reporting the session Spotify still has for
    # the account: only the status _play claimed is followed
    await session._handle_event(_device_event(is_active=True))
    await session._handle_event(_device_event(is_active=False))
    assert session.usable is True


async def test_a_reconnect_snapshot_keeps_an_active_session_alive(tmp_path: Path) -> None:
    """The events connection re-snapshots after a drop; that is not a device change."""
    session = _make_session(tmp_path)
    await session._handle_event(_auth_event(logged_in=True, is_active=True))
    await session._handle_event(_device_event(is_active=True))
    assert session.usable is True


async def test_the_playback_snapshots_active_flag_is_ignored(tmp_path: Path) -> None:
    """It is optional and rides on deltas, so only the dedicated reports are followed."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._current = session._open_channel(TRACK_A)
    await session._handle_event(
        SoloistEvent(
            type="playback_changed",
            data=SoloistPlaybackState(status="playing", is_active=False),
            raw={},
        )
    )
    assert session.usable is True


async def test_backpressure_does_not_spend_the_pause_budget(tmp_path: Path) -> None:
    """A sink suspended to cap the cushion is our doing, not the user pausing."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._engine_playing = True
    session._backpressured = True
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    await session._handle_event(_playback_event("paused"))
    _client_of(session).resume.assert_not_awaited()
    assert session._app_pauses == 0


async def test_a_lost_login_is_not_reported_as_a_takeover(tmp_path: Path) -> None:
    """Losing the login wins over the inactive device it brings with it."""
    session = _make_session(tmp_path)
    await session._handle_event(_auth_event(logged_in=False, is_active=False))
    assert session.usable is False
    assert session._app_control is None


async def test_a_track_started_from_the_app_ends_the_session(tmp_path: Path) -> None:
    """The engine pulled off an item part-way through is the app playing something else."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.duration_ms = 200_000
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    item.observe_position(20_000)

    await session._observe_current("spotify:track:theirs", 180_000, track_changed=True)
    assert session.usable is False
    assert session._app_control is SoloistAppControl.TOOK_OVER
    assert session.current is item


async def test_a_track_played_earlier_started_from_the_app_ends_the_session(
    tmp_path: Path,
) -> None:
    """A known uri is no exemption: only the item fed behind this one is where we sent it."""
    session = _make_session(tmp_path)
    played = session._open_channel(TRACK_B)
    played.spent = True
    item = session._open_channel(TRACK_A)
    item.duration_ms = 200_000
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    item.observe_position(20_000)

    await session._observe_current(TRACK_B, 180_000, track_changed=True)
    assert session.usable is False
    assert session._app_control is SoloistAppControl.TOOK_OVER


async def test_skipping_from_the_app_to_the_fed_item_is_followed(tmp_path: Path) -> None:
    """The queue moves to that same track, so following the engine keeps the two in step."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.duration_ms = 200_000
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    item.observe_position(20_000)
    fed = _feed(session, TRACK_B)

    await session._observe_current(TRACK_B, 180_000, track_changed=True)
    assert session.usable is True
    assert session.current is fed
    assert session.item_for(TRACK_B) is fed


async def test_a_skip_leaves_the_outgoing_stream_nothing_to_report(tmp_path: Path) -> None:
    """A jump Music Assistant asked for cuts the outgoing item, which is not starving."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.duration_ms = 200_000
    item.playing_seen = True
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    item.observe_position(20_000)
    fed = _feed(session, TRACK_B)
    # what skip_to arms before telling the engine to jump
    session._discard_until = fed

    await session._observe_current(TRACK_B, 180_000, track_changed=True)
    assert session.current is fed
    assert item.superseded
    await session.validate_item(item)


async def test_a_boundary_the_engine_drove_is_still_judged(tmp_path: Path) -> None:
    """Nobody asked the engine to leave this item, so what it delivered still counts."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.duration_ms = 200_000
    item.playing_seen = True
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    item.observe_position(20_000)
    _feed(session, TRACK_B)

    await session._observe_current(TRACK_B, 180_000, track_changed=True)
    assert item.superseded is False
    with pytest.raises(AudioError, match="incomplete"):
        await session.validate_item(item)


async def test_a_takeover_snapshot_stops_pinning_volume_and_options(tmp_path: Path) -> None:
    """Once the app has the session, the rest of its snapshot must not reach the daemon."""
    session = _make_session(tmp_path)
    session._demand_started = True
    item = session._open_channel(TRACK_A)
    item.duration_ms = 200_000
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    item.observe_position(20_000)

    await session._handle_event(
        SoloistEvent(
            type="playback_changed",
            data=SoloistPlaybackState(
                status="playing",
                item=SoloistEntity(uri="spotify:track:theirs", entity_type="track"),
                volume=40,
                options=SoloistPlaybackOptions(shuffle=True, repeat="context"),
            ),
            raw={},
        )
    )
    assert session.usable is False
    _client_of(session).set_volume.assert_not_awaited()
    _client_of(session).set_shuffle.assert_not_awaited()


async def test_the_engine_moving_on_at_a_track_end_is_not_a_takeover(tmp_path: Path) -> None:
    """An unasked-for item the engine reaches at a boundary is its own autoplay."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.duration_ms = 200_000
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    item.observe_position(200_000)

    await session._observe_current("spotify:track:autoplay", 180_000, track_changed=True)
    assert session.usable is True
    assert session.item_for("spotify:track:autoplay") is None


async def test_an_ended_item_says_what_the_app_did(tmp_path: Path) -> None:
    """The item's stream fails with the takeover, not a generic session error."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    await session._handle_event(_device_event(is_active=False))
    with pytest.raises(SoloistAppControlError) as err:
        await session.validate_item(item)
    assert err.value.translation_key == SoloistAppControl.TOOK_OVER.value
    assert isinstance(err.value, ProviderStreamLimitError)


async def test_a_session_being_torn_down_does_not_hold_off_the_next_one(tmp_path: Path) -> None:
    """Teardown pauses the daemon; that must not read as the user pausing."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._current = session._open_channel(TRACK_A)
    _feed(session, TRACK_B)
    session._stopped = True
    for _ in range(_MAX_APP_PAUSE_RESUMES + 1):
        await session._handle_event(_playback_event("playing"))
        await session._handle_event(_playback_event("paused"))
    await session._handle_event(_device_event(is_active=False))
    session.backend._raise_if_app_controlled()
    _client_of(session).resume.assert_not_awaited()


async def test_no_session_is_started_while_the_app_holds_the_last_one(tmp_path: Path) -> None:
    """A replacement would claim the Connect device straight back off the user."""
    backend = _make_backend(tmp_path)
    backend._note_app_control(SoloistAppControl.TOOK_OVER)
    with pytest.raises(SoloistAppControlError):
        await backend._acquire(TRACK_A, 0, _streamdetails_for(queue_id="player1", uri=TRACK_A))


async def test_the_hold_on_a_new_session_expires(tmp_path: Path) -> None:
    """Coming back to Music Assistant later plays again without any fuss."""
    backend = _make_backend(tmp_path)
    backend._note_app_control(SoloistAppControl.TOOK_OVER)
    backend._app_control_until = time.monotonic() - 1
    backend._raise_if_app_controlled()
    assert backend._held_by_app() is None


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
        raise SoloistAppControlError(provider, SoloistAppControl.TOOK_OVER)

    provider.backend = MagicMock(stream_spotify_uri=_refuse)
    streamdetails = MagicMock(
        media_type=MediaType.AUDIOBOOK,
        data={"chapters": [TRACK_A, TRACK_B, "spotify:track:ccc"], "chapters_data": []},
    )

    with pytest.raises(SoloistAppControlError):
        async for _ in provider.get_audio_stream(streamdetails):
            pass
    # the first chapter's refusal ends it: no chapter is skipped over
    assert calls == [TRACK_A]


def test_the_playback_device_is_named_apart_from_the_connect_one() -> None:
    """Two identically named devices in the Spotify app is what causes the takeovers."""
    # the Connect devices are named after their connected player via a template;
    # none of the renderings for a player carrying the historic "Music Assistant"
    # name may collide with the fixed playback device name
    assert all(
        resolve_publish_name(template, "Music Assistant") != SOLOIST_DEVICE_NAME
        for template in PUBLISH_NAME_TEMPLATES
    )


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
    session._current = session._open_channel(TRACK_A)
    session._open_channel(TRACK_B)
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
    session._current = session._open_channel(TRACK_A)
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
    streamed = _streamed(session)
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    follower = _queue_item(TRACK_B)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=3)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 3 else None
    queues.get_next_item.return_value = follower
    await session.feed_after(streamdetails, streamed)
    _client_of(session).add_to_queue.assert_awaited_once_with(TRACK_B)
    assert session.pending_item(TRACK_B) is not None
    assert session.has_pending is True


async def test_repeating_one_track_does_not_feed_the_engine(tmp_path: Path) -> None:
    """Repeat-one replays the item from the buffer the queue holds, so nothing is queued."""
    session = _make_session(tmp_path, queue_id="player1")
    streamed = _streamed(session)
    streamdetails = MagicMock(provider="spotify--test")
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    # repeat-one names the item being streamed as its own follower
    queues.get_next_item.return_value = playing
    assert await session.feed_after(streamdetails, streamed) is False
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_an_item_the_queue_resolved_elsewhere_is_not_fed(tmp_path: Path) -> None:
    """A track the queue will stream from another provider must not be queued here."""
    session = _make_session(tmp_path, queue_id="player1")
    streamed = _streamed(session)
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    # same track, but the queue already picked a different provider for it
    follower = _queue_item(TRACK_B, streamdetails=MagicMock(provider="tidal--x"))
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = follower
    await session.feed_after(streamdetails, streamed)
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_skipping_to_the_fed_item_keeps_the_session(tmp_path: Path) -> None:
    """A next-track lands on the item already fed, so the engine jumps instead of respawning."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    session._logged_in = True
    backend._session = session
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    # fed one ahead and not reached yet, which is where a next-track goes
    fed = _feed(session, TRACK_B)

    async def _engine_gets_there(**_kwargs: Any) -> None:
        await session._observe_current(TRACK_B, 200_000, track_changed=True)

    _client_of(session).skip_next.side_effect = _engine_gets_there
    got_session, got_item = await backend._acquire(
        TRACK_B, 0, _streamdetails_for(queue_id="player1", uri=TRACK_B)
    )
    # the same session, no respawn, and the item that was already queued
    assert got_session is session
    assert got_item is fed
    assert backend._session is session
    _client_of(session).skip_next.assert_awaited_once()


async def test_a_repeated_track_keeps_the_session(tmp_path: Path) -> None:
    """The second occurrence of a track is served by the session that played the first."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    backend._session = session
    # the first occurrence has been delivered and the second was fed behind it
    first = _streamed(session)
    first.release()
    second = _feed(session, TRACK_A)

    async def _engine_gets_there(**_kwargs: Any) -> None:
        await session._observe_current(TRACK_A, 200_000, track_changed=True)

    _client_of(session).skip_next.side_effect = _engine_gets_there
    got_session, got_item = await backend._acquire(
        TRACK_A, 0, _streamdetails_for(queue_id="player1", uri=TRACK_A)
    )
    assert got_session is session
    assert got_item is second
    assert backend._session is session


async def test_a_next_item_the_session_was_not_fed_is_queued_and_skipped_to(
    tmp_path: Path,
) -> None:
    """A queue reordered after the feed is served by sending the engine on, not by respawning."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    session._engine_playing = True
    backend._session = session
    # the engine moved on into the item it was fed, which the queue no longer wants
    stale = session._current = session._open_channel(TRACK_B)
    stale.started.set()

    async def _engine_gets_there(**_kwargs: Any) -> None:
        await session._observe_current(TRACK_C, 200_000, track_changed=True)

    _client_of(session).skip_next.side_effect = _engine_gets_there
    got_session, got_item = await backend._acquire(
        TRACK_C, 0, _streamdetails_for(queue_id="player1", uri=TRACK_C)
    )
    assert got_session is session
    assert got_item.uri == TRACK_C
    assert got_item.claimed is True
    _client_of(session).add_to_queue.assert_awaited_once_with(TRACK_C)
    _client_of(session).skip_next.assert_awaited_once()
    assert stale._closed is True


async def test_the_item_the_engine_is_on_is_never_jumped_to(tmp_path: Path) -> None:
    """A jump steps past the item, so the engine is never sent to what it already plays."""
    session = _make_session(tmp_path)
    session._engine_playing = True
    _streamed(session, TRACK_A).release()
    assert await session.feed_and_skip_to(TRACK_A) is None
    _client_of(session).add_to_queue.assert_not_awaited()
    _client_of(session).skip_next.assert_not_awaited()


async def test_a_session_delivering_an_item_is_never_sent_to_another(tmp_path: Path) -> None:
    """A jump would cut short the item being delivered, so capacity is reported instead."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    session._engine_playing = True
    backend._session = session
    # an item the engine is on and a stream is reading
    _streamed(session, TRACK_B, media_key=_streamdetails_for(uri=TRACK_B).uri)
    with pytest.raises(ProviderStreamLimitError):
        await backend._acquire(TRACK_C, 0, _streamdetails_for(queue_id="player1", uri=TRACK_C))
    _client_of(session).add_to_queue.assert_not_awaited()
    _client_of(session).skip_next.assert_not_awaited()
    assert backend._session is session
    assert session.usable is True


@pytest.mark.parametrize(
    "blocker",
    [
        # one skip steps one entry, so anything queued behind would be landed on
        "something_queued",
        # a stopped engine has nothing to skip out of
        "engine_stopped",
        # the engine would not take the jump
        "refused",
    ],
)
async def test_a_session_that_cannot_be_sent_on_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    """Where the engine's transport cannot reach the item, a fresh session serves it."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    session._engine_playing = blocker != "engine_stopped"
    backend._session = session
    # the engine is on an item whose audio has been handed over already
    _streamed(session, TRACK_B).release()
    if blocker == "something_queued":
        _feed(session, TRACK_A)
    if blocker == "refused":
        _client_of(session).skip_next.side_effect = SoloistError("no")
    _install_fake_binary_manager(monkeypatch)
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    monkeypatch.setattr(session, "stop", AsyncMock())
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_C, 0, _streamdetails_for(queue_id="player1", uri=TRACK_C))
    if blocker != "refused":
        _client_of(session).add_to_queue.assert_not_awaited()


async def test_a_jump_to_the_fed_item_that_misses_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A jump the engine will not take costs a respawn, not the item."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    session._client = AsyncMock()
    backend._session = session
    _streamed(session).release()
    _feed(session, TRACK_B)
    _client_of(session).skip_next.side_effect = SoloistError("no")
    _install_fake_binary_manager(monkeypatch)
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    monkeypatch.setattr(session, "stop", AsyncMock())
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_B, 0, _streamdetails_for(queue_id="player1", uri=TRACK_B))


async def test_a_skip_drops_what_arrives_while_the_command_is_in_flight(
    tmp_path: Path,
) -> None:
    """
    Audio captured between the skip command and the engine's answer is dropped.

    Only covers the marker's own window; what the pipeline still holds when the
    answer arrives is measured at the cut instead.
    """
    session = _make_session(tmp_path)
    leaving = session._current = session._open_channel(TRACK_A)
    leaving.started.set()
    target = _feed(session, TRACK_B)
    captured: list[bytes] = []

    async def _engine_gets_there(**_kwargs: Any) -> None:
        # the pipeline still holds the old track while the command is in flight
        session._write_if_wanted(b"\x01" * 32)
        await session._observe_current(TRACK_B, 200_000, track_changed=True)
        # from here on the audio really is the new item's
        session._write_if_wanted(b"\x02" * 32)

    _client_of(session).skip_next.side_effect = _engine_gets_there
    await session.skip_to(target)
    captured.extend(target._chunks)
    assert b"".join(captured) == b"\x02" * 32
    assert session._discard_until is None


async def test_a_skip_the_engine_never_reaches_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip that does not land is an error, not a wait for the track to end."""
    monkeypatch.setattr(soloist_backend, "_JUMP_TIMEOUT_S", 0.05)
    session = _make_session(tmp_path)
    fed = _feed(session, TRACK_B)
    with pytest.raises(AudioError, match="did not reach"):
        await session.skip_to(fed)


def test_a_jump_gives_up_while_the_queue_is_still_waiting() -> None:
    """A jump that will not land has to fail in time for a fresh session to serve the item."""
    assert _JUMP_TIMEOUT_S < BUFFER_READY_TIMEOUT


async def test_a_fed_item_the_engine_has_not_reached_is_not_served(tmp_path: Path) -> None:
    """Skipping to an already-fed item must not hand over a channel that fills later."""
    session = _make_session(tmp_path)
    # the engine is still on the track before it
    _streamed(session, TRACK_A)
    fed = _feed(session, TRACK_B)
    assert session.item_for(TRACK_B) is None
    # once the engine gets there it is servable
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
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
    item = session._current = session._open_channel(TRACK_A)
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


@pytest.mark.parametrize("state", ["fed", "reached", "being_read"])
async def test_an_already_known_item_is_not_fed_twice(tmp_path: Path, state: str) -> None:
    """An item the session was fed, or has already moved on to, is not queued again."""
    session = _make_session(tmp_path, queue_id="player1")
    streamed = _streamed(session)
    known = _feed(session, TRACK_B)
    if state != "fed":
        # the engine got there while the stream is still reading the item before it
        await session._observe_current(TRACK_B, 200_000, track_changed=True)
        assert session.current is known
    if state == "being_read":
        # and its own stream opened, which spends the channel
        known.claim()
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    queues.get_next_item.return_value = _queue_item(TRACK_B)
    assert await session.feed_after(streamdetails, streamed) is True
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_occurrences_of_one_track_are_served_in_the_order_they_were_fed(
    tmp_path: Path,
) -> None:
    """A track queued three times in a row hands each occurrence its own channel."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    second = _feed(session, TRACK_A)
    third = _feed(session, TRACK_A)
    # the engine has not moved yet, so the next occurrence is the one fed first
    assert session.pending_item(TRACK_A) is second
    first.duration_ms = 200_000
    first.observe_position(199_000)
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    assert session.current is second
    assert session.pending_item(TRACK_A) is third
    second.claim()
    second.duration_ms = 200_000
    second.observe_position(199_000)
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    assert session.current is third
    assert session.pending_item(TRACK_A) is None


async def test_a_track_played_earlier_is_not_answered_with_its_old_channel(
    tmp_path: Path,
) -> None:
    """A track that comes round again is the occurrence fed for it, not the one played."""
    session = _make_session(tmp_path)
    played = _feed(session, TRACK_B)
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    # the engine moves on, so that channel is over
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    # ... and the track comes round again later in the queue
    again = _feed(session, TRACK_B)
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    assert session.current is again
    assert played.closed is True


async def test_a_channel_nothing_can_read_is_not_kept(tmp_path: Path) -> None:
    """A channel the session moved past, with no stream on it, stops counting against the cap."""
    session = _make_session(tmp_path)
    passed_by = _feed(session, TRACK_B)
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    session._write_if_wanted(b"\x01" * 4096)
    assert session._retained_bytes() == 4096
    # the engine moves on again and no stream ever opened this one
    await session._observe_current(TRACK_C, 200_000, track_changed=True)
    assert passed_by.closed is True
    session._open_channel(TRACK_A)
    assert passed_by not in session._channels
    assert session._retained_bytes() == 0


async def test_a_channel_a_stream_still_holds_is_never_dropped(tmp_path: Path) -> None:
    """A stream still draining an item past the cut keeps the session in use."""
    session = _make_session(tmp_path)
    reading = _streamed(session)
    # the engine moves on while that stream is still reading the item
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    assert reading.closed is True
    assert reading.claimed is True
    session._open_channel(TRACK_C)
    assert reading in session._channels
    assert session.in_use is True


async def test_the_channel_the_engine_is_on_is_always_kept(tmp_path: Path) -> None:
    """The last item of a run is closed by its own drain, but the session is still on it."""
    session = _make_session(tmp_path)
    last = _streamed(session)
    last.release()
    last.close()
    session._open_channel(TRACK_B)
    assert session.current is last
    assert last in session._channels


async def test_a_cancelled_jump_ends_the_session_without_blaming_the_spotify_app(
    tmp_path: Path,
) -> None:
    """A jump nobody is waiting for any more ends the session, but is not a takeover."""
    session = _make_session(tmp_path)
    session._engine_playing = True
    playing = _streamed(session, TRACK_A)
    playing.release()
    # part-way through, so an arrival nobody asked for would read as a takeover
    playing.duration_ms = 200_000
    playing.observe_position(20_000)

    async def _gives_up(**_kwargs: Any) -> None:
        raise asyncio.CancelledError

    _client_of(session).skip_next.side_effect = _gives_up
    with pytest.raises(asyncio.CancelledError):
        await session.feed_and_skip_to(TRACK_B)
    # the jump cannot be accounted for any more, so the session ends
    assert session.usable is False
    # ... but the engine still getting there is not the app taking over, which
    # would hold off every session that follows
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    assert session._app_control is None
    assert session.backend._held_by_app() is None


async def test_a_drained_channel_is_not_served(tmp_path: Path) -> None:
    """The last item of a run ends its channel when its audio is done; it serves nothing after."""
    session = _make_session(tmp_path)
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    assert session.item_for(TRACK_A) is item
    # nothing follows it, so the session drains the item and closes it
    item.close()
    assert session.item_for(TRACK_A) is None


async def test_a_channel_the_session_moved_past_is_not_served(tmp_path: Path) -> None:
    """A channel the session left behind holds only part of its item, so it is never handed out."""
    session = _make_session(tmp_path)
    played_past = _feed(session, TRACK_B)
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    # servable while the engine is on it and no stream has taken it
    assert session.item_for(TRACK_B) is played_past
    # the engine moves on again before any stream opened it
    await session._observe_current(TRACK_C, 200_000, track_changed=True)
    assert played_past.closed is True
    assert session.item_for(TRACK_B) is None


async def test_a_repeated_track_is_fed_a_channel_of_its_own(tmp_path: Path) -> None:
    """A track that follows itself is queued again, not answered with the channel in use."""
    session = _make_session(tmp_path, queue_id="player1")
    streamed = _streamed(session)
    streamdetails = MagicMock()
    playing = _queue_item(TRACK_A, streamdetails=streamdetails)
    queues = _queues_of(session)
    queues.get.return_value = MagicMock(current_index=0)
    queues.get_item.side_effect = lambda _queue_id, index: playing if index == 0 else None
    # the very same track once more, as a queue item of its own
    queues.get_next_item.return_value = _queue_item(TRACK_A, queue_item_id="qi-again")
    assert await session.feed_after(streamdetails, streamed) is True
    _client_of(session).add_to_queue.assert_awaited_once_with(TRACK_A)
    second = session.pending_item(TRACK_A)
    assert second is not None
    assert second is not streamed


async def test_a_repeated_track_moves_on_at_its_track_change(tmp_path: Path) -> None:
    """The boundary between two occurrences of one track is reported under the same uri."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    first.duration_ms = 200_000
    first.observe_position(199_000)
    second = _feed(session, TRACK_A)
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    assert session.current is second
    assert first._closed is True


async def test_a_state_report_does_not_move_a_repeated_track_on(tmp_path: Path) -> None:
    """Only a track change crosses that boundary; a state report says where the engine is."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    first.duration_ms = 200_000
    first.observe_position(199_000)
    second = _feed(session, TRACK_A)
    await session._observe_current(TRACK_A, 200_000, track_changed=False)
    assert session.current is first
    assert session.pending_item(TRACK_A) is second


async def test_the_track_change_event_moves_a_repeated_track_on(tmp_path: Path) -> None:
    """The track_changed event is the one that carries a repeat across its boundary."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    first.duration_ms = 200_000
    first.observe_position(199_000)
    second = _feed(session, TRACK_A)
    await session._handle_event(_current_item_event("track_changed", TRACK_A, 200_000))
    assert session.current is second
    assert first.closed is True


@pytest.mark.parametrize("event_type", ["playback_state", "playback_changed"])
async def test_a_state_event_does_not_move_a_repeated_track_on(
    tmp_path: Path, event_type: str
) -> None:
    """A snapshot near the end of the first occurrence describes it, it does not end it."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    first.duration_ms = 200_000
    first.observe_position(199_000)
    second = _feed(session, TRACK_A)
    await session._handle_event(_current_item_event(event_type, TRACK_A, 200_000))
    assert session.current is first
    assert first.closed is False
    assert session.pending_item(TRACK_A) is second


@pytest.mark.parametrize("event_type", ["track_changed", "playback_state", "playback_changed"])
async def test_an_event_naming_another_item_cuts_at_the_boundary(
    tmp_path: Path, event_type: str
) -> None:
    """Whichever event reports the move, the item being left ends and the next one takes over."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    first.duration_ms = 200_000
    first.observe_position(20_000)
    second = _feed(session, TRACK_B)
    await session._handle_event(_current_item_event(event_type, TRACK_B, 180_000))
    # the engine left the previous item part-way through, but for one it was fed:
    # the queue moving on, not the Spotify app taking the session over
    assert session.usable is True
    assert session.current is second
    assert second.duration_ms == 180_000
    assert first.closed is True


async def test_a_position_report_tells_the_current_item_where_the_engine_is(
    tmp_path: Path,
) -> None:
    """Where the engine got to is what tells an item played out from one it was pulled off."""
    session = _make_session(tmp_path)
    item = _streamed(session)
    item.duration_ms = 200_000
    assert item.mid_play is False  # no position reported yet, so nothing to judge by
    await session._handle_event(_position_event(20_000))
    assert item.last_position_ms == 20_000
    assert item.mid_play is True


async def test_a_seek_in_flight_is_not_cut_short_by_a_repeat_boundary(tmp_path: Path) -> None:
    """A channel opened for a seek has no position of its own, which is not a played-out one."""
    session = _make_session(tmp_path)
    seeking = _streamed(session)
    seeking.duration_ms = 200_000
    _feed(session, TRACK_A)
    session._seeking = True
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    assert session.current is seeking
    assert seeking.closed is False


async def test_a_repeat_is_not_moved_on_to_part_way_through_the_first(tmp_path: Path) -> None:
    """A track change reported part-way through the first occurrence is not its boundary."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    first.duration_ms = 200_000
    first.observe_position(20_000)
    _feed(session, TRACK_A)
    await session._observe_current(TRACK_A, 200_000, track_changed=True)
    assert session.current is first


async def test_a_jump_to_a_repeat_is_followed_part_way_through(tmp_path: Path) -> None:
    """Skipping ahead to the second occurrence lands there even mid-track."""
    session = _make_session(tmp_path)
    first = _streamed(session)
    first.duration_ms = 200_000
    first.observe_position(20_000)
    second = _feed(session, TRACK_A)

    async def _engine_gets_there(**_kwargs: Any) -> None:
        await session._observe_current(TRACK_A, 200_000, track_changed=True)

    _client_of(session).skip_next.side_effect = _engine_gets_there
    await session.skip_to(second)
    assert session.current is second
    assert first._closed is True


async def test_only_tracks_are_fed_ahead(tmp_path: Path) -> None:
    """A podcast episode or audiobook chapter is played on its own, never stitched."""
    session = _make_session(tmp_path, queue_id="player1")
    await session.feed_after(MagicMock(), session._open_channel("spotify:episode:xyz"))
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_a_non_spotify_follower_is_not_fed(tmp_path: Path) -> None:
    """The run simply ends where the queue leaves this provider."""
    session = _make_session(tmp_path, queue_id="player1")
    streamed = _streamed(session)
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
    await session.feed_after(streamdetails, streamed)
    _client_of(session).add_to_queue.assert_not_awaited()


async def test_a_library_item_is_fed_through_its_spotify_mapping(tmp_path: Path) -> None:
    """A library track is fed with the item id this provider instance knows it by."""
    session = _make_session(tmp_path, queue_id="player1")
    streamed = _streamed(session)
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
    await session.feed_after(streamdetails, streamed)
    _client_of(session).add_to_queue.assert_awaited_once_with(TRACK_B)


@pytest.mark.parametrize(
    ("provider_option", "player_setting", "expected"),
    [
        (True, "enabled", True),
        # the player's own switch decides first: off means nobody normalizes,
        # not that the job passes to Spotify
        (True, "disabled", False),
        (False, "enabled", False),
        (False, "disabled", False),
    ],
)
def test_who_normalizes_needs_both_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_option: bool,
    player_setting: str,
    expected: bool,
) -> None:
    """The engine normalizes only when the provider option and the player agree."""
    session = _make_session(tmp_path, queue_id="player1")
    monkeypatch.setattr(
        type(session.backend.provider),
        "spotify_normalization_configured",
        property(lambda _self: provider_option),
    )
    cast("MagicMock", session.mass.config).get_effective_player_queue_config_value = MagicMock(
        return_value=player_setting
    )
    assert session._engine_normalization_enabled() is expected


def test_a_running_session_answers_for_what_the_engine_is_doing(tmp_path: Path) -> None:
    """
    The engine reads its settings at startup, so a later toggle must not split them.

    Otherwise the streams core would start normalizing on top of audio the engine
    is still normalizing, or stop while it no longer is.
    """
    backend = _make_backend(tmp_path)
    provider = backend.provider
    streamdetails = _streamdetails_for(queue_id="player1")
    # nothing playing yet: the configuration is all there is to go on
    before_any_session = backend.session_normalizes(streamdetails)
    session = _SoloistSession(backend, "player1")
    session.engine_normalizes = True
    backend._session = session
    while_playing = backend.session_normalizes(streamdetails)
    # ... and a session that has been torn down no longer speaks for the engine
    session._stopped = True
    after_teardown = backend.session_normalizes(streamdetails)
    assert before_any_session is None
    assert while_playing is True
    assert after_teardown is None
    assert (
        provider.delivers_normalized_audio(streamdetails)
        is provider.spotify_normalization_configured
    )


async def test_short_delivery_is_rejected_as_incomplete(tmp_path: Path) -> None:
    """PCM that stops well short of the item's duration is rejected."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.playing_seen = True
    item.duration_ms = 200_000
    item.last_position_ms = 100_000
    with pytest.raises(AudioError, match="incomplete"):
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


@pytest.mark.parametrize(
    "playing_seen",
    [
        True,
        # a second seek cuts a channel before the engine gets to it, which is
        # still a channel that was replaced rather than an item that failed
        False,
    ],
    ids=["was_playing", "never_started"],
)
async def test_a_superseded_item_is_not_judged(tmp_path: Path, playing_seen: bool) -> None:
    """A channel cut part-way is short on purpose, so it is no evidence of starving."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.playing_seen = playing_seen
    item.duration_ms = 200_000
    item.last_position_ms = 30_000
    item.close(superseded=True)
    await session.validate_item(item)


async def test_an_item_the_engine_moved_on_from_keeps_its_verdict(tmp_path: Path) -> None:
    """A later teardown must not excuse a channel the engine already starved."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.playing_seen = True
    item.duration_ms = 200_000
    item.last_position_ms = 30_000
    # the boundary the engine drove, with the teardown following behind it
    item.close()
    await session.stop()
    with pytest.raises(AudioError, match="incomplete"):
        await session.validate_item(item)


def test_an_unread_session_expires(tmp_path: Path) -> None:
    """A session no item stream reads from is ended so its daemon does not linger."""
    session = _make_session(tmp_path)
    session._open_channel(TRACK_A)
    session._expire_idle()
    assert session._idle_since is not None
    assert session.usable is True
    session._idle_since = time.monotonic() - _IDLE_TIMEOUT_S - 1
    session._expire_idle()
    assert session.usable is False


def test_a_session_being_read_never_expires(tmp_path: Path) -> None:
    """An item stream reading the session keeps it alive indefinitely."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.claim()
    session._idle_since = time.monotonic() - _IDLE_TIMEOUT_S * 10
    session._expire_idle()
    assert session.usable is True


def test_pre_roll_silence_is_dropped_a_whole_frame_at_a_time() -> None:
    """
    Trimming pre-roll must leave the audio on the session's frame grid.

    A FIFO read is not always a whole number of frames, and dropping a partial
    one would shift every sample that follows for the rest of the session.
    """
    shaper = _CaptureShaper()
    # pre-roll that ends mid-frame: the real audio starts at byte 1024
    assert shaper.shape(b"\x00" * 1021) == b""
    audio = bytes(range(1, 9)) * 4
    shaped = shaper.shape(b"\x00" * 3 + audio)
    assert shaped == audio
    assert shaper._lead_skipped % _FRAME_BYTES == 0


async def test_a_refused_skip_does_not_leave_the_audio_discarded(tmp_path: Path) -> None:
    """
    A skip that never landed must not keep the session dropping its audio.

    The marker silences everything the session captures, so a command that
    failed has to clear it on the way out.
    """
    session = _make_session(tmp_path)
    client = cast("MagicMock", session._client)
    client.skip_next = AsyncMock(side_effect=TimeoutError)
    item = _ItemAudio(TRACK_B, session)

    with pytest.raises(AudioError, match="would not skip"):
        await session.skip_to(item)

    assert session._discard_until is None


async def test_a_daemon_that_will_not_die_is_reported_and_released(tmp_path: Path) -> None:
    """A close that could not terminate the daemon still finishes the teardown."""
    session = _make_session(tmp_path)
    proc = cast("MagicMock", session._proc)
    proc.close = AsyncMock()
    # AsyncProcess.close() gives up after a handful of kill attempts
    proc.returncode = None
    with patch.object(session.logger, "warning") as warning:
        await session.stop()
    assert warning.called
    assert session._teardown_done is True
    assert session._proc is None


async def test_a_cancelled_teardown_still_closes_the_daemon(tmp_path: Path) -> None:
    """
    A cancelled teardown must leave the retry something to close.

    Dropping the references first is how a daemon survives to hold the data
    directory, which every later session is then refused for.
    """
    session = _make_session(tmp_path)
    proc = cast("MagicMock", session._proc)
    sink = cast("AsyncMock", session._sink)

    async def _never_returns() -> None:
        await asyncio.Event().wait()

    proc.close = _never_returns
    task = asyncio.create_task(session.stop())
    await asyncio.sleep(0.01)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    # the teardown did not finish, so nothing was dropped and it can be redone
    unfinished = session._teardown_done
    kept_proc = session._proc
    kept_sink = session._sink
    proc.close = AsyncMock()
    proc.returncode = 0
    await session.stop()
    assert unfinished is False
    assert kept_proc is proc
    assert kept_sink is sink
    assert session._teardown_done is True
    assert session._proc is None
    assert session._sink is None
    proc.close.assert_awaited()
    sink.unload.assert_awaited()


async def test_a_teardown_leaves_the_running_stream_nothing_to_report(tmp_path: Path) -> None:
    """Stopping the session cuts the item being played; that is not a starved item."""
    session = _make_session(tmp_path)
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    item.duration_ms = 260_000
    item.playing_seen = True
    item.observe_position(30_000)
    await session.stop()

    assert item.superseded
    await session.validate_item(item)


def test_a_failed_session_is_torn_down(tmp_path: Path) -> None:
    """A session that fails is discarded, so its daemon does not keep playing to nobody."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
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
    item = session._open_channel(TRACK_A)
    item.claim()
    # the engine is playing something else, so nothing is ever written here
    session._current = session._open_channel("spotify:track:other")
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


async def test_a_skip_drops_the_audio_still_in_flight(tmp_path: Path) -> None:
    """The item jumped to opens with its own audio, not the tail of the one left behind."""
    session = _make_session(tmp_path)
    left_behind = session._open_channel(TRACK_A)
    session._current = left_behind
    left_behind.started.set()
    left_behind.claim()
    jumped_to = _feed(session, TRACK_B)
    session._discard_until = jumped_to
    with _capture_holding(session, fifo_bytes=2 * _FRAME_BYTES, reader_bytes=2 * _FRAME_BYTES):
        await session._observe_current(TRACK_B, 200_000, track_changed=True)
    assert session._stale_budget == 4 * _FRAME_BYTES
    session._write_if_wanted(b"s" * (4 * _FRAME_BYTES))
    session._write_if_wanted(b"n" * (2 * _FRAME_BYTES))
    jumped_to.claim()
    jumped_to.close()
    assert b"".join([chunk async for chunk in jumped_to.read()]) == b"n" * (2 * _FRAME_BYTES)


async def test_a_skip_drops_the_stale_audio_across_reads(tmp_path: Path) -> None:
    """A budget larger than one read keeps dropping, and resumes on a frame boundary."""
    session = _make_session(tmp_path)
    session._stale_budget = 3 * _FRAME_BYTES
    item = session._current = session._open_channel(TRACK_A)
    item.claim()
    session._write_if_wanted(b"s" * (2 * _FRAME_BYTES))
    session._write_if_wanted(b"s" * _FRAME_BYTES + b"n" * _FRAME_BYTES)
    item.close()
    assert b"".join([chunk async for chunk in item.read()]) == b"n" * _FRAME_BYTES


async def test_the_marker_spends_an_earlier_jumps_budget(tmp_path: Path) -> None:
    """What the marker drops still counts against a budget left from an earlier jump."""
    session = _make_session(tmp_path)
    session._current = session._open_channel(TRACK_A)
    session._stale_budget = 4 * _FRAME_BYTES
    session._discard_until = _feed(session, TRACK_B)
    session._write_if_wanted(b"s" * (3 * _FRAME_BYTES))
    assert session._stale_budget == _FRAME_BYTES
    # a refused command leaves only what is genuinely still in flight to drop
    session._discard_until = None
    session._write_if_wanted(b"s" * _FRAME_BYTES + b"n" * _FRAME_BYTES)
    item = session._current
    item.claim()
    item.close()
    assert b"".join([chunk async for chunk in item.read()]) == b"n" * _FRAME_BYTES


async def test_a_natural_cut_keeps_the_audio_in_flight(tmp_path: Path) -> None:
    """Nothing is dropped without a jump: what is in flight is the continuation."""
    session = _make_session(tmp_path)
    playing = session._open_channel(TRACK_A)
    session._current = playing
    playing.started.set()
    playing.claim()
    with _capture_holding(session, fifo_bytes=4 * _FRAME_BYTES, reader_bytes=4 * _FRAME_BYTES):
        await session._observe_current(TRACK_B, 200_000, track_changed=True)
    assert session._stale_budget == 0


def test_stale_bytes_spans_both_buffers_in_whole_frames(tmp_path: Path) -> None:
    """The in-flight measure covers the FIFO and the reader, and never splits a frame."""
    session = _make_session(tmp_path)
    with _capture_holding(session, fifo_bytes=3 * _FRAME_BYTES + 3, reader_bytes=2 * _FRAME_BYTES):
        assert session._stale_bytes() == 5 * _FRAME_BYTES


def test_stale_bytes_falls_back_when_the_reader_cannot_be_sized(tmp_path: Path) -> None:
    """Losing the reader's internal view drops extra rather than leaving audio behind."""
    session = _make_session(tmp_path)
    with _capture_holding(session, fifo_bytes=0, reader_bytes=None):
        assert session._stale_bytes() == 6 * _READ_CHUNK_SIZE


async def test_a_channel_abandoned_at_the_cut_stops_holding_the_cushion(
    tmp_path: Path,
) -> None:
    """A skip closes the channel first and only then unwinds its stream."""
    session = _make_session(tmp_path)
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    item.claim()
    item.write(b"x" * 4096)
    # the cut lands while the abandoned stream is still unwinding
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    assert session._retained_bytes() == 4096
    item.release()
    assert session._retained_bytes() == 0


def test_an_abandoned_channel_stops_holding_the_cushion(tmp_path: Path) -> None:
    """A channel skipped away from frees its buffer instead of gating the sink for good."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.claim()
    item.write(b"x" * 4096)
    assert session._retained_bytes() == 4096
    # the stream is gone, then the cut closes the channel
    item.release()
    item.close()
    assert session._retained_bytes() == 0


async def test_a_channel_no_stream_ever_took_stops_holding_the_cushion(
    tmp_path: Path,
) -> None:
    """A channel the session cuts with nothing reading it frees its buffer right away."""
    session = _make_session(tmp_path)
    item = _feed(session, TRACK_B)
    await session._observe_current(TRACK_B, 200_000, track_changed=True)
    session._write_if_wanted(b"\x01" * 4096)
    assert session._retained_bytes() == 4096
    # still the current channel, so the prune cannot be what frees the cushion
    item.close()
    assert item in session._channels
    assert session._retained_bytes() == 0


async def test_a_channel_still_being_read_keeps_its_tail(tmp_path: Path) -> None:
    """Closing the playing item at a cut must not discard what its stream is still owed."""
    session = _make_session(tmp_path)
    item = session._open_channel(TRACK_A)
    item.claim()
    item.write(b"tail" * 4)
    item.close()
    assert item.buffered == 16
    assert b"".join([chunk async for chunk in item.read()]) == b"tail" * 4


@contextmanager
def _capture_holding(
    session: _SoloistSession, *, fifo_bytes: int, reader_bytes: int | None
) -> Iterator[None]:
    """
    Give the session a real capture FIFO and a reader holding the given amounts.

    A real pipe is used so the byte count comes from the same ioctl the backend
    relies on. Pass ``reader_bytes=None`` for a reader whose buffer cannot be read.
    """
    read_fd, write_fd = os.pipe()
    try:
        if fifo_bytes:
            os.write(write_fd, bytes(fifo_bytes))
        pipe = MagicMock()
        pipe.fileno.return_value = read_fd
        transport = MagicMock()
        transport.get_extra_info.return_value = pipe
        session._transport = transport
        reader = MagicMock(spec=[]) if reader_bytes is None else MagicMock()
        if reader_bytes is not None:
            reader._buffer = bytearray(reader_bytes)
        session._reader = reader
        yield
    finally:
        session._transport = None
        session._reader = None
        os.close(read_fd)
        os.close(write_fd)


def _stdout_of(*lines: str) -> MagicMock:
    """Return a process mock whose stdout yields the given daemon log lines."""

    async def _iter_stdout() -> AsyncGenerator[str]:
        for line in lines:
            yield line

    proc = MagicMock()
    proc.iter_stdout = _iter_stdout
    return proc


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
    # a session under test is past the engine's login and has claimed the
    # Connect device, unless a test says otherwise
    session._logged_in = True
    session._was_active = True
    return session


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


def _feed(session: _SoloistSession, uri: str) -> _ItemAudio:
    """Return the channel of an item handed to the engine that it has not started."""
    item = session._open_channel(uri)
    session._pending.append(item)
    return item


def _streamed(
    session: _SoloistSession, uri: str = TRACK_A, media_key: str | None = None
) -> _ItemAudio:
    """Return the channel of the item the engine plays and a stream is reading."""
    item = session._current = session._open_channel(uri)
    item.started.set()
    item.claim(media_key)
    return item


def _make_item(tmp_path: Path, uri: str) -> _ItemAudio:
    """Return a bare item channel on a mocked session."""
    return _ItemAudio(uri, _make_session(tmp_path))


def _queue_item(uri: str, streamdetails: Any = None, queue_item_id: str | None = None) -> MagicMock:
    """Return a queue item stand-in for a Spotify track on the test instance."""
    item_id = uri.rsplit(":", 1)[1]
    media_item = MagicMock(media_type=MediaType.TRACK, provider="spotify--test", item_id=item_id)
    media_item.provider_mappings = []
    return MagicMock(
        media_item=media_item,
        queue_item_id=queue_item_id or f"qi-{item_id}",
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


def _current_of(session: _SoloistSession) -> _ItemAudio:
    """Return the channel the session is playing, which the caller knows exists."""
    item = session._current
    assert item is not None
    return item


def _sink_of(session: _SoloistSession) -> AsyncMock:
    """Return the session's mocked capture sink."""
    return cast("AsyncMock", session._sink)


def _queues_of(session: _SoloistSession) -> MagicMock:
    """Return the mocked player_queues controller the session consults."""
    return cast("MagicMock", session.mass.player_queues)


def _auth_event(*, logged_in: bool, is_active: bool = True) -> SoloistEvent:
    """Return an auth_state event with the given login and active-device state."""
    return SoloistEvent(
        type="auth_state",
        data=SoloistAuthState(logged_in=logged_in, is_active=is_active),
        raw={},
    )


def _device_event(*, is_active: bool) -> SoloistEvent:
    """Return a device_changed event with the given active-device state."""
    return SoloistEvent(
        type="device_changed", data=SoloistDeviceChanged(is_active=is_active), raw={}
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


def _position_event(position_ms: int) -> SoloistEvent:
    """Return a position_sync event reporting the given playback position."""
    return SoloistEvent(
        type="position_sync",
        data=SoloistPositionSync(position=SoloistPosition(position_ms=position_ms, timestamp_ms=0)),
        raw={},
    )


def _current_item_event(event_type: str, uri: str, duration_ms: int | None = None) -> SoloistEvent:
    """
    Return an event reporting the given item as the one the engine is on.

    :param event_type: ``track_changed``, ``playback_state`` or ``playback_changed``.
    :param uri: The Spotify URI the event names.
    :param duration_ms: The duration to decorate the item with, when it has one.
    """
    item = SoloistEntity(
        uri=uri,
        entity_type="track",
        decorations={"playback": {"duration_ms": duration_ms}} if duration_ms else {},
    )
    if event_type == "track_changed":
        return SoloistEvent(type=event_type, data=SoloistTrackChanged(item=item), raw={})
    return SoloistEvent(
        type=event_type, data=SoloistPlaybackState(status="playing", item=item), raw={}
    )


def _install_fake_binary_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the shared binary manager so no download or exec is attempted."""
    manager = MagicMock()
    manager.ensure_fresh = AsyncMock(return_value=Path("/nonexistent/soloist"))
    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", MagicMock(return_value=manager))


async def test_seeking_the_playing_item_keeps_the_session(tmp_path: Path) -> None:
    """The engine is moved where it stands rather than the session being respawned."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.claim()
    playing.duration_ms = 260_000
    playing.playing_seen = True
    playing.observe_position(30_000)
    # the pre-seek audio nobody may hear again
    playing.write(b"\x01" * 32)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    item = await session.seek_current(TRACK_A, 120_000)

    assert item is not playing
    assert session.current is item
    assert item.claimed
    # what the track is stays with it; where it was does not
    assert item.duration_ms == 260_000
    assert item.playing_seen
    assert item.started_at_ms == 120_000
    # the outgoing channel is closed, which is what ends the stream reading it
    assert playing._closed
    _client_of(session).seek.assert_awaited_once_with(120_000, await_result=True)


async def test_a_seek_leaves_the_outgoing_stream_nothing_to_report(tmp_path: Path) -> None:
    """
    The superseded channel validates clean, so an ordinary seek stays out of the log.

    Its stream is still attached and validates the channel when it ends, and the
    engine is nowhere near the end of an item being seeked away from.
    """
    session = _make_session(tmp_path)
    playing = _streamed(session)
    playing.duration_ms = 260_000
    playing.playing_seen = True
    playing.observe_position(30_000)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    await session.seek_current(TRACK_A, 120_000)
    playing.release()

    assert playing.superseded
    await session.validate_item(playing)


async def test_a_seek_of_the_playing_item_is_sent_only_once(tmp_path: Path) -> None:
    """
    A landed seek is never repeated: a repeat restarts the item, audibly.

    The engine answers late on purpose, so a re-send loop around the wait would
    have fired several times over before the confirmation arrives.
    """
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.observe_position(30_000)
    pending: list[asyncio.Task[None]] = []

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        async def _confirm_late() -> None:
            await asyncio.sleep(0.05)
            _current_of(session).observe_position(position_ms)

        pending.append(asyncio.create_task(_confirm_late()))

    _client_of(session).seek.side_effect = _engine_seeks
    with patch.object(soloist_backend, "_SEEK_RETRY_INTERVAL_S", 0.01):
        await session.seek_current(TRACK_A, 120_000)
    await asyncio.gather(*pending)
    assert _client_of(session).seek.await_count == 1


async def test_seeking_back_is_not_confirmed_by_the_position_seeked_away_from(
    tmp_path: Path,
) -> None:
    """A report still describing the pre-seek position cannot land a backward seek."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.duration_ms = 260_000
    playing.observe_position(200_000)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        item = _current_of(session)
        # a report from before the seek is still in flight; it sits above the
        # target's tolerance window and must not pass for the landing
        item.observe_position(200_000)
        assert not item.seek_confirmed.is_set()
        item.observe_position(position_ms)
        item.observe_position(position_ms + 2)

    _client_of(session).seek.side_effect = _engine_seeks
    item = await session.seek_current(TRACK_A, 60_000)
    assert item.started_at_ms == 60_002
    # and the pre-seek report is not left standing in for progress this item
    # never made, which at_own_end and the completeness check would believe
    assert item.last_position_ms == 60_002


async def test_audio_in_flight_across_an_in_place_seek_is_dropped(tmp_path: Path) -> None:
    """Only audio from past the seek reaches the fresh channel."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.observe_position(30_000)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        # still rendering the position being left behind
        session._write_if_wanted(b"\x01" * 32)
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    with _capture_holding(session, fifo_bytes=2 * _FRAME_BYTES, reader_bytes=_FRAME_BYTES):
        item = await session.seek_current(TRACK_A, 120_000)
    # nothing rendered while the engine was being moved reached the channel
    assert not item._chunks
    # and what the pipeline still held at the confirmation is dropped after it
    assert session._stale_budget == 3 * _FRAME_BYTES
    session._write_if_wanted(b"\x02" * (3 * _FRAME_BYTES))
    session._write_if_wanted(b"\x03" * 16)
    assert b"".join(item._chunks) == b"\x03" * 16


async def test_the_sink_is_suspended_while_a_seek_is_in_flight(tmp_path: Path) -> None:
    """No pre-seek audio enters the capture while the engine is being moved."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._engine_playing = True
    session._sink_running = True
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.observe_position(30_000)
    suspended_during_seek = False

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        nonlocal suspended_during_seek
        suspended_during_seek = not session._sink_running
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    await session.seek_current(TRACK_A, 120_000)
    assert suspended_during_seek
    _sink_of(session).suspend.assert_awaited()


async def test_a_seek_the_engine_never_confirms_fails_the_item(tmp_path: Path) -> None:
    """An unconfirmed seek is reported rather than served from the wrong position."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.observe_position(30_000)
    with (
        patch.object(soloist_backend, "_SEEK_CONFIRM_TIMEOUT_S", 0.01),
        pytest.raises(AudioError, match="did not confirm"),
    ):
        await session.seek_current(TRACK_A, 120_000)


async def test_a_refused_seek_command_reports_soloist(tmp_path: Path) -> None:
    """A rejected seek names the engine, so the caller can fall back."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    _client_of(session).seek.side_effect = SoloistError("nope")
    with pytest.raises(AudioError, match="would not seek"):
        await session.seek_current(TRACK_A, 120_000)


async def test_seeking_an_item_the_engine_is_not_on_is_refused(tmp_path: Path) -> None:
    """Only the item the session is actually playing can be seeked in place."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    with pytest.raises(AudioError, match="is not playing"):
        await session.seek_current(TRACK_B, 120_000)


async def test_a_seek_of_the_playing_item_is_served_by_the_running_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session is seeked where it stands instead of being replaced."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _make_session(tmp_path)
    backend._session = session
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    # its own stream is still attached when the seek re-opens it
    item.claim()
    item.observe_position(30_000)
    stopped = AsyncMock()
    monkeypatch.setattr(session, "stop", stopped)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    got_session, got_item = await backend._acquire(
        TRACK_A, 90, _streamdetails_for(queue_id="player1", uri=TRACK_A)
    )

    assert got_session is session
    assert got_item is not item
    assert got_item.claimed
    stopped.assert_not_awaited()
    _client_of(session).seek.assert_awaited_once_with(90_000, await_result=True)


async def test_a_cancelled_seek_does_not_leave_the_session_wedged(tmp_path: Path) -> None:
    """
    A superseded seek ends the session instead of holding it claimed for good.

    A second seek cancels the stream the first one is being made for, and the
    channel it had already claimed would otherwise keep the session in use:
    unable to expire, and refusing every later item as busy.
    """
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.observe_position(30_000)
    seeking = asyncio.create_task(session.seek_current(TRACK_A, 120_000))
    # let it get as far as waiting for the engine to confirm
    while not _client_of(session).seek.await_count:
        await asyncio.sleep(0)
    seeking.cancel()
    with suppress(asyncio.CancelledError):
        await seeking
    assert not session.usable
    assert not session._seeking


async def test_a_seek_cancelled_before_the_channel_is_swapped_keeps_the_session(
    tmp_path: Path,
) -> None:
    """Nothing has been given up yet while the sink is still being held."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    held = asyncio.Event()

    async def _slow_suspend(**_kwargs: Any) -> None:
        held.set()
        await asyncio.sleep(60)

    with patch.object(session, "_apply_sink_state", _slow_suspend):
        seeking = asyncio.create_task(session.seek_current(TRACK_A, 120_000))
        await held.wait()
        seeking.cancel()
        with suppress(asyncio.CancelledError):
            await seeking
    # the session is untouched and, crucially, not left dropping every chunk
    assert session.usable
    assert not session._seeking
    assert session.current is playing


async def test_a_seek_is_refused_once_the_engine_has_moved_on(tmp_path: Path) -> None:
    """The item seeked must still be the one the engine is on when the sink settles."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    follower = session._open_channel(TRACK_B)

    async def _boundary_lands(**_kwargs: Any) -> None:
        session._current = follower

    with (
        patch.object(session, "_apply_sink_state", _boundary_lands),
        pytest.raises(AudioError, match="moved on from"),
    ):
        await session.seek_current(TRACK_A, 120_000)
    # the follower the engine actually reached keeps its own channel
    assert session.current is follower
    # ... and the refused seek opened no channel of its own
    assert [item for item in session._channels if item.uri == TRACK_A] == [playing]


async def test_a_seek_that_fails_part_way_restarts_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seek the engine refuses after the channel was swapped still gets its audio."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _make_session(tmp_path)
    backend._session = session
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    item.claim()
    _client_of(session).seek.side_effect = SoloistError("refused")
    stopped = AsyncMock()
    monkeypatch.setattr(session, "stop", stopped)
    _install_fake_binary_manager(monkeypatch)
    monkeypatch.setattr(
        soloist_backend._SoloistSession, "start", AsyncMock(side_effect=AudioError("spawn"))
    )
    with pytest.raises(AudioError, match="spawn"):
        await backend._acquire(TRACK_A, 90, _streamdetails_for(queue_id="player1", uri=TRACK_A))
    stopped.assert_awaited_once()


async def test_a_seek_refused_because_the_app_took_over_does_not_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement would claim the Connect device back off the Spotify app."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _make_session(tmp_path)
    backend._session = session
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    item.claim()
    item.observe_position(30_000)
    started = AsyncMock()
    monkeypatch.setattr(soloist_backend._SoloistSession, "start", started)

    async def _app_takes_over(_position_ms: int, **_kwargs: Any) -> None:
        # the user moved playback elsewhere from their Spotify app while the
        # seek was in flight; the wait is released by the session ending
        session._end_on_app_control(SoloistAppControl.TOOK_OVER)

    _client_of(session).seek.side_effect = _app_takes_over
    with pytest.raises(SoloistAppControlError):
        await backend._acquire(TRACK_A, 120, _streamdetails_for(queue_id="player1", uri=TRACK_A))
    started.assert_not_awaited()


async def test_a_seek_is_abandoned_when_the_app_took_over_during_the_suspend(
    tmp_path: Path,
) -> None:
    """Nothing is seeked on a session the Spotify app has already taken over."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()

    async def _app_takes_over(**_kwargs: Any) -> None:
        session._end_on_app_control(SoloistAppControl.TOOK_OVER)

    with (
        patch.object(session, "_apply_sink_state", _app_takes_over),
        pytest.raises(SoloistAppControlError),
    ):
        await session.seek_current(TRACK_A, 120_000)
    # the engine was never asked to move
    _client_of(session).seek.assert_not_awaited()


async def test_a_cancelled_sink_transition_is_re_issued(tmp_path: Path) -> None:
    """
    A suspend that may or may not have landed is never taken as done.

    A sink that did suspend would otherwise still read as running, and the
    resume that should follow would be skipped as a no-op: silence for good.
    """
    session = _make_session(tmp_path)
    session._demand_started = True
    session._engine_playing = True
    session._sink_running = True
    session._seeking = True

    async def _cancelled_suspend() -> None:
        raise asyncio.CancelledError

    _sink_of(session).suspend.side_effect = _cancelled_suspend
    with suppress(asyncio.CancelledError):
        await session._apply_sink_state()
    # the engine plays on and the sink is wanted running again
    session._seeking = False
    await session._apply_sink_state()
    _sink_of(session).resume.assert_awaited_once()


async def test_a_seek_cancelled_at_the_final_resume_does_not_leave_the_session_usable(
    tmp_path: Path,
) -> None:
    """The channel is claimed by then, so an abandoned seek must still end the session."""
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.observe_position(30_000)
    calls = 0
    real_apply = session._apply_sink_state

    async def _cancel_on_the_way_out(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError
        await real_apply(**kwargs)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    with (
        patch.object(session, "_apply_sink_state", _cancel_on_the_way_out),
        suppress(asyncio.CancelledError),
    ):
        await session.seek_current(TRACK_A, 120_000)
    assert not session.usable


async def test_a_cold_seek_does_not_read_a_failed_session_as_landed(tmp_path: Path) -> None:
    """The wake-up a fatal failure gives every channel is not a confirmed seek."""
    session = _make_session(tmp_path)
    item = session._current = session._open_channel(TRACK_A)

    async def _engine_dies(_position_ms: int, **_kwargs: Any) -> None:
        session._fail("the session exited")

    _client_of(session).seek.side_effect = _engine_dies
    with pytest.raises(AudioError, match="the session exited"):
        await session._cold_seek(_client_of(session), item, 60_000)


async def test_seeking_the_playing_item_back_to_its_start_keeps_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A seek to zero is a seek: reachable once an earlier one moved the buffer.

    The buffer only hands a position to the provider when it cannot serve it
    itself, so seeking back before an earlier seek's target arrives here with a
    target of zero.
    """
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _make_session(tmp_path)
    backend._session = session
    item = session._current = session._open_channel(TRACK_A)
    item.started.set()
    item.claim()
    item.observe_position(200_000)
    stopped = AsyncMock()
    monkeypatch.setattr(session, "stop", stopped)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        current = _current_of(session)
        current.observe_position(position_ms)
        current.observe_position(position_ms + 2)

    _client_of(session).seek.side_effect = _engine_seeks
    got_session, got_item = await backend._acquire(
        TRACK_A, 0, _streamdetails_for(queue_id="player1", uri=TRACK_A)
    )

    assert got_session is session
    assert got_item is not item
    stopped.assert_not_awaited()
    _client_of(session).seek.assert_awaited_once_with(0, await_result=True)
    assert got_item.started_at_ms == 2


async def test_a_short_forward_seek_still_confirms(tmp_path: Path) -> None:
    """
    A seek only a little past where the engine is must not wait itself out.

    Reachable because the engine runs ahead of what has been delivered - up to
    the retained cushion - so a target the buffer will not serve can still be
    inside the tolerance window of the engine's own position. Demanding the
    engine drop below that mark would never be satisfied by a seek forward.
    """
    session = _make_session(tmp_path)
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.duration_ms = 260_000
    # the engine is at 49s while only ~30s has been delivered
    playing.observe_position(49_000)

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    item = await session.seek_current(TRACK_A, 50_500)
    assert item.seek_confirmed.is_set()
    assert item.started_at_ms == 50_500


async def test_a_seek_does_not_arm_the_last_items_drain(tmp_path: Path) -> None:
    """
    The channel opened for a seek has no position yet, which is not an ended item.

    Holding the sink can make the engine report a state that is not playing, and
    the run's last item would then be drained out from under the seek.
    """
    session = _make_session(tmp_path)
    session._demand_started = True
    playing = session._current = session._open_channel(TRACK_A)
    playing.started.set()
    playing.duration_ms = 260_000
    playing.observe_position(30_000)
    armed: list[bool] = []

    async def _engine_seeks(position_ms: int, **_kwargs: Any) -> None:
        # nothing to judge the fresh channel by yet, and no follower queued
        await session._handle_playback_state(
            SoloistPlaybackState(status="buffering", item=None, position=None)
        )
        armed.append(_current_of(session).draining)
        _current_of(session).observe_position(position_ms)

    _client_of(session).seek.side_effect = _engine_seeks
    item = await session.seek_current(TRACK_A, 120_000)
    assert armed == [False]
    assert not item.draining


async def test_the_item_a_finished_run_stopped_on_is_not_seeked_in_place(
    tmp_path: Path,
) -> None:
    """
    A matching uri is not proof the engine is still playing it.

    The channel stays current through the idle grace after the run ended, and a
    seek would wait out its confirmation on an engine that has stopped.
    """
    session = _make_session(tmp_path)
    ended = session._current = session._open_channel(TRACK_A)
    ended.started.set()
    ended.close()
    with pytest.raises(AudioError, match="is not playing"):
        await session.seek_current(TRACK_A, 0)
    _client_of(session).seek.assert_not_awaited()
