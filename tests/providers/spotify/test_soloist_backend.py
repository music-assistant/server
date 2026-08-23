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
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import AudioError, LoginFailed

from music_assistant.helpers.pulse_capture import CAPTURE_SAMPLE_RATE
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
    SoloistEvent,
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


async def test_an_abandoned_channel_cannot_be_continued(tmp_path: Path) -> None:
    """A stream abandoned mid-item marks its channel broken so the session restarts."""
    session = _make_session(tmp_path)
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    assert session.item_for(TRACK_A) is item
    item.claim()
    item.release()
    assert item.broken is True
    assert session.item_for(TRACK_A) is None


async def test_a_completed_channel_is_not_broken(tmp_path: Path) -> None:
    """A channel released after its normal end is not marked broken."""
    session = _make_session(tmp_path)
    item = _ItemAudio(TRACK_A, session)
    item.claim()
    item.close()
    item.release()
    assert item.broken is False


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


async def test_auth_event_records_logout(tmp_path: Path) -> None:
    """An auth_state event with logged_in=False fails the session for re-pairing."""
    session = _make_session(tmp_path)
    await session._handle_event(
        SoloistEvent(
            type="auth_state", data=SoloistAuthState(logged_in=False, is_active=False), raw={}
        )
    )
    assert session._logged_out is True
    assert session.usable is False


async def test_buffering_gates_the_sink_once_demand_started(tmp_path: Path) -> None:
    """Once PCM demand started, buffering suspends the sink and playing resumes it."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    session._pending.append(TRACK_B)
    sink = _sink_of(session)
    await session._handle_event(_playback_event("buffering"))
    sink.suspend.assert_awaited_once()
    sink.resume.assert_not_awaited()
    await session._handle_event(_playback_event("playing"))
    sink.resume.assert_awaited_once()
    assert session._current is not None
    assert session._current.playing_seen is True


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


async def test_the_last_item_is_drained_rather_than_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause on the run's last item drains its tail, then closes it and the sink."""
    monkeypatch.setattr(soloist_backend, "_DRAIN_TIMEOUT_S", 0.01)
    session = _make_session(tmp_path)
    session._demand_started = True
    item = session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    item.duration_ms = 1_000
    sink = _sink_of(session)
    await session._handle_event(_playback_event("paused"))
    # the sink stays open for now, so audio still in the FIFO can arrive...
    sink.suspend.assert_not_awaited()
    assert item.draining is True
    assert item._closed is False
    # ... but only that item's own audio is taken, never the padding silence
    # the sink keeps rendering after it
    item.write(b"\x01" * (1_000 * CAPTURE_SAMPLE_RATE // 1000 * _FRAME_BYTES))
    item.write(b"\x00" * 4096)
    assert item._buffered == 1_000 * CAPTURE_SAMPLE_RATE // 1000 * _FRAME_BYTES
    await asyncio.sleep(0.05)
    sink.suspend.assert_awaited_once()
    assert item._closed is True


async def test_a_pause_with_more_queued_suspends_the_sink(tmp_path: Path) -> None:
    """A pause while another item is queued behind is ordinary interference, not the end."""
    session = _make_session(tmp_path)
    session._demand_started = True
    session._items[TRACK_A] = session._current = _ItemAudio(TRACK_A, session)
    session._pending.append(TRACK_B)
    await session._handle_event(_playback_event("paused"))
    _sink_of(session).suspend.assert_awaited_once()


async def test_a_second_player_does_not_steal_a_session_in_use(tmp_path: Path) -> None:
    """One session serves one player: a second player is refused, not handed the session."""
    backend = _make_backend(tmp_path)
    backend._server = MagicMock()
    backend._binary = Path("/nonexistent/soloist")
    session = _SoloistSession(backend, "player1")
    backend._session = session
    item = session._items[TRACK_A] = _ItemAudio(TRACK_A, session)
    item.claim()
    with pytest.raises(AudioError, match="one player at a time"):
        await backend._acquire(TRACK_B, 0, "player2")
    # the session that was playing is untouched
    assert backend._session is session
    assert session.usable is True


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
    follower = MagicMock(media_item=MagicMock(media_type=MediaType.TRACK, provider="tidal--x"))
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
        media_item=MagicMock(media_type=MediaType.TRACK, provider="library", item_id="42")
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
    # the quality tier is not managed here, so the engine keeps its own
    assert not any(line.startswith("audio.play_bitrate") for line in content)


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


def _client_of(session: _SoloistSession) -> AsyncMock:
    """Return the session's mocked WebSocket client."""
    return cast("AsyncMock", session._client)


def _sink_of(session: _SoloistSession) -> AsyncMock:
    """Return the session's mocked capture sink."""
    return cast("AsyncMock", session._sink)


def _queues_of(session: _SoloistSession) -> MagicMock:
    """Return the mocked player_queues controller the session consults."""
    return cast("MagicMock", session.mass.player_queues)


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
