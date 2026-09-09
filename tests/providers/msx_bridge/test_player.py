"""Tests for MSXPlayer."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import Mock, patch

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import CONF_HTTP_PROFILE
from music_assistant.providers.msx_bridge.player import MSXPlayer
from tests.providers.msx_bridge.factories import queue_item as make_queue_item

# --- Initialization and properties ---


def _update_state_mock(player: MSXPlayer) -> Mock:
    """Return the update-state mock installed by the player fixture."""
    return cast("Mock", player.update_state)


def _player_media(uri: str, **kwargs: Any) -> PlayerMedia:
    """Create a concrete media record with only the test-relevant fields."""
    return PlayerMedia(uri=uri, **kwargs)


def test_init_defaults(player: MSXPlayer) -> None:
    """MSXPlayer should have correct default attributes."""
    assert player._attr_name == "Test TV"
    assert player._attr_type == PlayerType.PLAYER
    assert isinstance(player._prepare_lock, asyncio.Lock)
    assert PlayerFeature.PAUSE in player._attr_supported_features
    assert PlayerFeature.SET_MEMBERS not in player._attr_supported_features
    assert PlayerFeature.VOLUME_SET in player._attr_supported_features
    assert player._attr_available is True
    assert player._attr_powered is True
    assert player._attr_volume_level == 100
    assert player.output_format == "mp3"
    assert player.requires_flow_mode is False


def test_init_custom_params(provider: Any) -> None:
    """MSXPlayer should accept custom name and output_format."""
    p = MSXPlayer(provider, "msx_custom", name="Living Room TV", output_format="flac")
    cast("Any", p).update_state = Mock()
    assert p._attr_name == "Living Room TV"
    assert p.output_format == "flac"


async def test_http_profile_defaults_to_forced_content_length(player: MSXPlayer) -> None:
    """Redirected MA streams must expose a finite length for MSX progress."""
    entries = await player.get_config_entries()

    http_profile = next(entry for entry in entries if entry.key == CONF_HTTP_PROFILE)
    assert http_profile.default_value == "forced_content_length"


def test_needs_poll_always_true(player: MSXPlayer) -> None:
    """needs_poll should always return True."""
    assert player.needs_poll is True


def test_poll_interval_playing(player: MSXPlayer) -> None:
    """poll_interval should return 5 when playing."""
    player._attr_playback_state = PlaybackState.PLAYING
    assert player.poll_interval == 5


def test_poll_interval_not_playing(player: MSXPlayer) -> None:
    """poll_interval should return 30 when idle or paused."""
    player._attr_playback_state = PlaybackState.IDLE
    assert player.poll_interval == 30
    player._attr_playback_state = PlaybackState.PAUSED
    assert player.poll_interval == 30


def test_ws_connect_marks_player_available(player: MSXPlayer) -> None:
    """A reconnect makes a previously unavailable TV available to MA again."""
    player._attr_available = False

    player.on_ws_connected()

    assert player.available is True
    _update_state_mock(player).assert_called_once()


def test_mark_available_restores_offline_player(player: MSXPlayer) -> None:
    """HTTP activity can restore a player that was marked unavailable."""
    player._attr_available = False

    player.mark_available()

    assert player.available is True
    _update_state_mock(player).assert_called_once()


def test_ws_disconnect_marks_playing_player_unavailable(player: MSXPlayer) -> None:
    """A playing TV that loses its only WebSocket must become unavailable."""
    player._attr_playback_state = PlaybackState.PLAYING

    player.on_ws_disconnected()

    assert player.available is False
    _update_state_mock(player).assert_called_once()


# --- Playback ---


async def test_play_media(player: MSXPlayer) -> None:
    """play_media should store stream URL, set state to PLAYING, and reset elapsed."""
    media = _player_media("http://ma-server/stream/12345")

    await player.play_media(media)

    assert player.current_stream_url == "http://ma-server/stream/12345"
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_elapsed_time == 0
    assert player._attr_elapsed_time_last_updated is not None
    assert player._attr_current_media is media
    _update_state_mock(player).assert_called()


async def test_play_media_sets_media_ready_event(player: MSXPlayer) -> None:
    """play_media should set _media_ready event so wait_for_media returns immediately."""
    media = _player_media("http://ma-server/stream/12345")

    assert not player._media_ready.is_set()
    await player.play_media(media)
    assert player._media_ready.is_set()


async def test_wait_for_media_returns_on_play(player: MSXPlayer) -> None:
    """wait_for_media should return the media once play_media sets the event."""
    media = _player_media("http://ma-server/stream/12345")

    async def delayed_play() -> None:
        await asyncio.sleep(0.05)
        await player.play_media(media)

    task = asyncio.create_task(delayed_play())
    result = await player.wait_for_media(timeout=2.0)
    assert result is media
    await task


async def test_wait_for_media_fast_path(player: MSXPlayer) -> None:
    """wait_for_media should return immediately if play_media already ran."""
    media = _player_media("http://ma-server/stream/12345")

    # Simulate: queue.play_media already called player.play_media
    await player.play_media(media)
    assert player._media_ready.is_set()

    # Fast path — should return instantly without clearing the event
    result = await player.wait_for_media(timeout=0.1)
    assert result is media


async def test_wait_for_media_timeout(player: MSXPlayer) -> None:
    """wait_for_media should return None on timeout."""
    result = await player.wait_for_media(timeout=0.1)
    assert result is None


async def test_stop_does_not_clear_media_ready_event(player: MSXPlayer) -> None:
    """
    stop() must NOT clear _media_ready (C1 fix).

    Clearing it in stop() would race with a concurrent wait_for_media() call.
    The wait_for_media() fast-path already guards on _attr_current_media, so
    leaving the event set is safe.
    """
    player._media_ready.set()
    await player.stop()
    # _attr_current_media is None after stop — wait_for_media returns None even
    # though the event may still be set.
    assert player._attr_current_media is None
    result = await player.wait_for_media(timeout=0.05)
    assert result is None


async def test_expect_new_media_arms_wait_for_media(player: MSXPlayer) -> None:
    """
    After expect_new_media(), wait_for_media must wait for the NEXT play_media.

    Without arming, the event left set by a previous track would make
    wait_for_media return the stale current_media immediately — serving the
    previous track's stream to the TV.
    """
    old_media = _player_media("library://track/1")
    await player.play_media(old_media)

    new_media = _player_media("library://track/2")
    player.expect_new_media()

    async def delayed_play() -> None:
        await asyncio.sleep(0.05)
        await player.play_media(new_media)

    task = asyncio.create_task(delayed_play())
    result = await player.wait_for_media(timeout=2.0)
    assert result is new_media
    await task


async def test_expect_new_media_timeout_returns_none(player: MSXPlayer) -> None:
    """After expect_new_media(), wait_for_media times out with None if no play_media arrives."""
    old_media = _player_media("library://track/1")
    await player.play_media(old_media)

    player.expect_new_media()
    result = await player.wait_for_media(timeout=0.05)
    assert result is None


async def test_play_resume(player: MSXPlayer) -> None:
    """play() when PAUSED should notify MSX to resume and set state to PLAYING."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 42.0

    with patch.object(player.provider, "notify_play_resumed") as mock_notify:
        await player.play()

    assert player._attr_playback_state == PlaybackState.PLAYING
    mock_notify.assert_called_once_with(player.player_id)


async def test_pause_accumulates_time(player: MSXPlayer) -> None:
    """pause() should accumulate elapsed time from last update."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = 100.0

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 115.0
        await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    assert player._attr_elapsed_time == 25.0  # 10 + (115 - 100)
    _update_state_mock(player).assert_called()


async def test_pause_none_elapsed(player: MSXPlayer) -> None:
    """pause() should not crash when elapsed_time is None."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None

    await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    # elapsed stays None since there was nothing to accumulate
    assert vars(player)["_attr_elapsed_time"] is None


async def test_pause_notifies_pause_on_msx(player: MSXPlayer) -> None:
    """pause() should call provider.notify_play_paused so MSX pauses the player."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = 100.0

    with patch.object(player.provider, "notify_play_paused") as mock_notify:
        await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    mock_notify.assert_called_once_with(player.player_id)


async def test_stop_clears_all(player: MSXPlayer) -> None:
    """stop() should reset state, media, elapsed, and stream URL."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_current_media = _player_media("library://track/1")
    cast("Any", player)._attr_elapsed_time = 42.0
    cast("Any", player).current_stream_url = "http://something"

    await player.stop()

    assert player._attr_playback_state == PlaybackState.IDLE
    state = vars(player)
    assert state["_attr_current_media"] is None
    assert state["_attr_elapsed_time"] is None
    assert state["_attr_elapsed_time_last_updated"] is None
    assert state["current_stream_url"] is None
    _update_state_mock(player).assert_called()


async def test_stop_idempotent(player: MSXPlayer) -> None:
    """Calling stop() on an idle player should not raise."""
    player._attr_playback_state = PlaybackState.IDLE
    await player.stop()
    assert player._attr_playback_state == PlaybackState.IDLE


# --- Volume and polling ---


async def test_volume_set(player: MSXPlayer) -> None:
    """volume_set should update volume level and call update_state."""
    await player.volume_set(75)
    assert player._attr_volume_level == 75
    _update_state_mock(player).assert_called()


async def test_poll_updates_elapsed(player: MSXPlayer) -> None:
    """poll() should accumulate elapsed time during PLAYING."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = 200.0

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 205.0
        await player.poll()

    assert player._attr_elapsed_time == 15.0  # 10 + (205 - 200)
    assert player._attr_elapsed_time_last_updated == 205.0
    _update_state_mock(player).assert_called()


async def test_poll_noop_when_paused(player: MSXPlayer) -> None:
    """poll() should not update anything when paused."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 42.0
    _update_state_mock(player).reset_mock()

    await player.poll()

    assert player._attr_elapsed_time == 42.0
    _update_state_mock(player).assert_not_called()


async def test_poll_noop_when_idle(player: MSXPlayer) -> None:
    """poll() should not update anything when idle."""
    player._attr_playback_state = PlaybackState.IDLE
    _update_state_mock(player).reset_mock()

    await player.poll()

    _update_state_mock(player).assert_not_called()


# --- Queue-backed playlist playback ---


async def test_play_media_queue_sends_playlist(player: MSXPlayer, mass_mock: Mock) -> None:
    """play_media with queue context should send playlist via WS instead of stream."""
    media = _player_media(
        "http://ma-server/stream/12345",
        title="Track 1",
        artist="Artist 1",
        duration=180,
        source_id="msx_test",
        queue_item_id="qi1",
    )

    queue = PlayerQueue(
        queue_id="msx_test",
        active=True,
        display_name="Test queue",
        available=True,
        items=5,
        current_index=2,
    )

    mass_mock.player_queues.get.return_value = queue
    mass_mock.player_queues.get_item.return_value = None
    mass_mock.player_queues.items.return_value = [
        make_queue_item(queue_item_id=f"qi{index}") for index in range(5)
    ]

    with (
        patch.object(player.provider, "notify_play_playlist") as mock_playlist,
        patch.object(player.provider, "notify_play_started") as mock_play,
    ):
        await player.play_media(media)

    mock_playlist.assert_called_once_with("msx_test", 2, queue_id="msx_test")
    mock_play.assert_not_called()
    assert player._playing_from_queue is True
    assert player._playlist_offset == 2
    assert player._playlist_size == 5
    mass_mock.player_queues.items.assert_called_once_with("msx_test", limit=5)


async def test_play_media_reloads_playlist_when_playing_from_queue(
    player: MSXPlayer, mass_mock: Mock
) -> None:
    """Same-queue play reloads a rotated playlist so MSX index 0 is the current item."""
    player._playing_from_queue = True
    player._queue_source_id = "msx_test"
    player._playlist_offset = 2
    player._playlist_size = 5

    media = _player_media(
        "http://ma-server/stream/12345", source_id="msx_test", queue_item_id="qi2"
    )

    queue = PlayerQueue(
        queue_id="msx_test",
        active=True,
        display_name="Test queue",
        available=True,
        items=5,
        current_index=3,
    )

    mass_mock.player_queues.get.return_value = queue
    mass_mock.player_queues.get_item.return_value = None
    mass_mock.player_queues.items.return_value = [
        make_queue_item(queue_item_id=f"qi{index}") for index in range(5)
    ]

    with (
        patch.object(player.provider, "notify_goto_index") as mock_goto,
        patch.object(player.provider, "notify_play_playlist") as mock_playlist,
        patch.object(player.provider, "notify_play_started") as mock_play,
    ):
        await player.play_media(media)

    mock_goto.assert_not_called()
    mock_playlist.assert_called_once_with("msx_test", 3, queue_id="msx_test")
    mock_play.assert_not_called()
    mass_mock.player_queues.items.assert_called_once_with("msx_test", limit=5)


async def test_play_media_skips_ws_when_skip_notify_set(player: MSXPlayer, mass_mock: Mock) -> None:
    """play_media should skip all WS notifications when _skip_ws_notify is True."""
    player._playing_from_queue = True
    player._skip_ws_notify = True

    media = _player_media(
        "http://ma-server/stream/12345", source_id="msx_test", queue_item_id="qi2"
    )

    mass_mock.player_queues.get_item.return_value = None

    with (
        patch.object(player.provider, "notify_goto_index") as mock_goto,
        patch.object(player.provider, "notify_play_playlist") as mock_playlist,
        patch.object(player.provider, "notify_play_started") as mock_play,
    ):
        await player.play_media(media)

    mock_goto.assert_not_called()
    mock_playlist.assert_not_called()
    mock_play.assert_not_called()

    # Clean up
    player._skip_ws_notify = False


async def test_play_media_non_queue_sends_broadcast_play(
    player: MSXPlayer,
) -> None:
    """play_media without queue context should push the media metadata via broadcast_play."""
    media = _player_media(
        "http://ma-server/stream/12345",
        title="Track 1",
        artist="Artist 1",
        image_url="http://ma-server/image.png",
        duration=180,
    )

    with (
        patch.object(player.provider, "notify_play_playlist") as mock_playlist,
        patch.object(player.provider, "notify_play_started") as mock_play,
    ):
        await player.play_media(media)

    mock_playlist.assert_not_called()
    mock_play.assert_called_once_with(
        player.player_id,
        title="Track 1",
        artist="Artist 1",
        image_url="http://ma-server/image.png",
        duration=180,
        next_action=f"execute:/api/next/{player.player_id}",
        prev_action=f"execute:/api/previous/{player.player_id}",
    )


async def test_stop_resets_playing_from_queue(player: MSXPlayer) -> None:
    """stop() should reset _playing_from_queue flag."""
    player._playing_from_queue = True
    await player.stop()
    assert player._playing_from_queue is False


# --- WebSocket position reporting ---


async def test_update_position_ignores_stale_report_after_track_change(
    player: MSXPlayer,
) -> None:
    """A leftover TV clock must not keep MA progress on the previous track."""
    media = _player_media("library://track/1", duration=180)
    with patch.object(player.provider, "notify_play_started"):
        await player.play_media(media)
    player.update_position(95.0)
    assert player._attr_elapsed_time == 0.0
    player.update_position(0.4)
    assert player._attr_elapsed_time == 0.4


async def test_update_position_accepts_report_after_seek(player: MSXPlayer) -> None:
    """Seek must rebase the stale-position baseline so later reports stay valid."""
    media = _player_media("library://track/1", duration=180)
    with (
        patch.object(player.provider, "notify_play_started"),
        patch.object(player.provider, "notify_seek"),
    ):
        await player.play_media(media)
        await player.seek(120)
    player.update_position(121.0)
    assert player._attr_elapsed_time == 121.0


async def test_note_tv_seek_trusts_jump_before_first_report(player: MSXPlayer) -> None:
    """A TV seek before any position report must not be treated as stale."""
    media = _player_media("library://track/1", duration=180)
    with patch.object(player.provider, "notify_play_started"):
        await player.play_media(media)
    player.note_tv_seek(80.0)
    assert player._attr_elapsed_time == 80.0
    player.update_position(81.0)
    assert player._attr_elapsed_time == 81.0


async def test_note_tv_seek_updates_position_while_paused(player: MSXPlayer) -> None:
    """A native seek while paused must update MA without trusting later position reports."""
    media = _player_media("library://track/1", duration=180)
    with patch.object(player.provider, "notify_play_started"):
        await player.play_media(media)
    player._attr_playback_state = PlaybackState.PAUSED

    player.note_tv_seek(80.0)
    player.update_position(90.0)

    assert player._attr_elapsed_time == 80.0


async def test_update_position_rebases_after_tv_seek(player: MSXPlayer) -> None:
    """A forward jump after a fresh report is a TV seek, not a stale clock."""
    media = _player_media("library://track/1", duration=180)
    with patch.object(player.provider, "notify_play_started"):
        await player.play_media(media)
    player.update_position(1.0)
    player.update_position(80.0)
    assert player._attr_elapsed_time == 80.0


def test_suppress_ws_notify_nests(player: MSXPlayer) -> None:
    """Overlapping suppress contexts must stay suppressed until the last exit."""
    with player.suppress_ws_notify():
        assert player._skip_ws_notify is True
        with player.suppress_ws_notify():
            assert player._skip_ws_notify is True
        assert player._skip_ws_notify is True
    assert player._skip_ws_notify is False


def test_update_position(player: MSXPlayer) -> None:
    """update_position should set elapsed_time and mark WS timestamp when PLAYING."""
    player._attr_playback_state = PlaybackState.PLAYING
    player.update_position(42.5)
    assert player._attr_elapsed_time == 42.5
    assert player._attr_elapsed_time_last_updated is not None
    assert player._last_ws_position is not None
    _update_state_mock(player).assert_called()


def test_update_position_clamps_to_served_stream_duration(player: MSXPlayer) -> None:
    """Position reports must not exceed the shortened stream served after a seek."""
    media = _player_media("library://track/1", duration=300, stream_duration=120)
    player._attr_current_media = media
    player._attr_playback_state = PlaybackState.PLAYING

    player.update_position(150)

    assert player._attr_elapsed_time == 120


def test_update_position_ignored_when_paused(player: MSXPlayer) -> None:
    """update_position should be ignored when PAUSED to protect accumulated time."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 45.0
    _update_state_mock(player).reset_mock()

    player.update_position(99.0)

    # elapsed_time should remain at 45.0, not be overwritten to 99.0
    assert player._attr_elapsed_time == 45.0
    _update_state_mock(player).assert_not_called()


async def test_poll_skips_when_ws_position_recent(player: MSXPlayer) -> None:
    """poll() should skip wall-clock increment when WS position was reported recently."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 30.0
    player._attr_elapsed_time_last_updated = 200.0
    player._last_ws_position = 200.0  # very recent (monotonic)

    _update_state_mock(player).reset_mock()

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 205.0
        mock_time.monotonic.return_value = 205.0  # only 5s since last WS (< 10s threshold)
        await player.poll()

    # Should NOT have updated elapsed_time
    assert player._attr_elapsed_time == 30.0
    _update_state_mock(player).assert_not_called()


async def test_poll_uses_wall_clock_when_ws_stale(player: MSXPlayer) -> None:
    """poll() should use wall-clock when WS position is stale (>10s ago)."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 30.0
    player._attr_elapsed_time_last_updated = 200.0
    player._last_ws_position = 180.0  # 25s ago (monotonic)

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 205.0
        mock_time.monotonic.return_value = 205.0
        await player.poll()

    assert player._attr_elapsed_time == 35.0  # 30 + (205 - 200)


async def test_poll_clamps_to_served_stream_duration(player: MSXPlayer) -> None:
    """Wall-clock progress must stop at the shortened stream served after a seek."""
    media = _player_media("library://track/1", duration=300, stream_duration=120)
    player._attr_current_media = media
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 115.0
    player._attr_elapsed_time_last_updated = 200.0
    player._last_ws_position = None

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 210.0
        await player.poll()

    assert player._attr_elapsed_time == 120


async def test_poll_ws_staleness_immune_to_wall_clock_jump(player: MSXPlayer) -> None:
    """
    A wall-clock jump (NTP step) must not make a fresh WS position look stale.

    The WS staleness check must use the monotonic clock: with wall-clock, an
    NTP correction of +1h right after a WS report makes poll() fall back to
    the wall-clock delta and corrupt elapsed_time by hours.
    """
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 30.0

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 200.0
        mock_time.monotonic.return_value = 1000.0
        player.update_position(42.0)

        # NTP jumps wall clock forward 1 hour; monotonic advances only 5s
        mock_time.time.return_value = 200.0 + 3600.0
        mock_time.monotonic.return_value = 1005.0
        await player.poll()

    # WS report is 5s old (monotonic) — still fresh, elapsed must be untouched
    assert player._attr_elapsed_time == 42.0


async def test_stop_clears_ws_position(player: MSXPlayer) -> None:
    """stop() should clear _last_ws_position."""
    player._last_ws_position = 100.0
    await player.stop()
    assert player._last_ws_position is None


# --- Resume from pause ---


async def test_resume_sends_ws_resume(player: MSXPlayer) -> None:
    """play() when PAUSED should notify MSX to resume native player."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 42.0

    with patch.object(player.provider, "notify_play_resumed") as mock_notify:
        await player.play()

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_elapsed_time_last_updated is not None
    mock_notify.assert_called_once_with(player.player_id)


async def test_resume_skips_ws_when_skip_notify(player: MSXPlayer) -> None:
    """play() when PAUSED with _skip_ws_notify should not broadcast to MSX."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 10.0
    player._skip_ws_notify = True

    with patch.object(player.provider, "notify_play_resumed") as mock_notify:
        await player.play()

    assert player._attr_playback_state == PlaybackState.PLAYING
    mock_notify.assert_not_called()
    player._skip_ws_notify = False


async def test_pause_skips_ws_when_skip_notify(player: MSXPlayer) -> None:
    """pause() with _skip_ws_notify should not broadcast to MSX."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = 100.0
    player._skip_ws_notify = True

    with patch.object(player.provider, "notify_play_paused") as mock_notify:
        await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    mock_notify.assert_not_called()
    player._skip_ws_notify = False
